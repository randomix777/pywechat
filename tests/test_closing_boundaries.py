"""本轮收尾的业务边界：草稿保护、短消息合并、统计口径、限流与恢复。

这些是"做错了会直接让商户吃亏"的边界，不是覆盖率数字：
* 覆盖用户正在输入的内容；
* 一笔订单被拆成三笔、三次扣费、三条回执；
* 改一次量让备货翻倍；
* 恢复发送时把过期的"已记单"发出去。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from wechat_ai_assistant.dashboard import (  # noqa: E402
    ACTIONABLE_STATUSES,
    EFFECTIVE_STATUSES,
    METRIC_DEFINITIONS,
    DashboardBuilder,
)
from wechat_ai_assistant.history_read import SimulatedHistory  # noqa: E402
from wechat_ai_assistant.local_parse import ParseProgress  # noqa: E402
from wechat_ai_assistant.merging import (  # noqa: E402
    BufferedMessage,
    MergeKey,
    MergeSettings,
    MessageMerger,
)
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.receipts import ReceiptOutbox, ReceiptRequest, ReceiptStatus  # noqa: E402
from wechat_ai_assistant.runmode import AutoOrderSettings, RunMode  # noqa: E402
from wechat_ai_assistant.wechat_send import (  # noqa: E402
    SendHeld,
    SendVerdict,
    StaticInspector,
    TargetState,
    mark_real_sending_verified,
    real_sending_available,
)
from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
GROUP = "测试群"


def uid() -> str:
    return uuid.uuid4().hex[:12]


# ======================================================= 1. 草稿保护


class DraftProtectionTests(unittest.TestCase):
    """发送前必须确认不会破坏用户正在输入的内容。"""

    def test_real_sending_is_off_until_verified(self):
        mark_real_sending_verified(False)
        self.addCleanup(mark_real_sending_verified, False)
        self.assertFalse(real_sending_available())
        sent: list[tuple[str, str]] = []
        guard = _guard(sent, TargetState(conversation=GROUP, observed=True, window_ready=True), allow_real=False)
        verdict = guard.send(GROUP, "好的，已记单")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, SendVerdict.REAL_SENDING_DISABLED)
        self.assertEqual(sent, [], "未验证前不得真的操作微信")

    def test_non_empty_draft_holds_the_receipt(self):
        sent: list[tuple[str, str]] = []
        guard = _guard(sent, TargetState(conversation=GROUP, draft_text="我明天要", observed=True, window_ready=True))
        verdict = guard.send(GROUP, "好的，已记单")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, SendVerdict.DRAFT_PRESENT)
        self.assertEqual(sent, [], "输入框有内容时绝不能发送")
        self.assertIn("暂缓", verdict.message)

    def test_whitespace_only_draft_does_not_count(self):
        sent: list[tuple[str, str]] = []
        guard = _guard(sent, TargetState(conversation=GROUP, draft_text="   \n ", observed=True, window_ready=True))
        self.assertTrue(guard.send(GROUP, "好的").ok)
        self.assertEqual(len(sent), 1)

    def test_wrong_conversation_holds(self):
        sent: list[tuple[str, str]] = []
        guard = _guard(sent, TargetState(conversation="别的群", observed=True, window_ready=True))
        verdict = guard.send(GROUP, "好的，已记单")
        self.assertEqual(verdict.reason, SendVerdict.WRONG_CONVERSATION)
        self.assertEqual(sent, [])

    def test_unreadable_state_holds(self):
        """读不到状态就是不能确认，不能确认就不发。"""
        sent: list[tuple[str, str]] = []
        guard = _guard(sent, TargetState(observed=False))
        verdict = guard.send(GROUP, "好的")
        self.assertEqual(verdict.reason, SendVerdict.STATE_UNKNOWN)
        self.assertEqual(sent, [])

    def test_account_mismatch_holds(self):
        sent: list[tuple[str, str]] = []
        guard = _guard(
            sent,
            TargetState(conversation=GROUP, account_id="wxid_other", observed=True, window_ready=True),
            expected_account="wxid_mine",
        )
        verdict = guard.send(GROUP, "好的")
        self.assertEqual(verdict.reason, SendVerdict.ACCOUNT_MISMATCH)
        self.assertEqual(sent, [])

    def test_merchant_typing_mid_check_is_noticed(self):
        """检查通过之后、真正发送之前，商户开始打字了 —— 必须放弃发送。

        这是"不靠先保存再还原"的关键：第二次核对才发现就不发，
        而不是把用户新打的字覆盖掉。
        """
        sent: list[tuple[str, str]] = []
        states = [TargetState(conversation=GROUP, observed=True, window_ready=True),
                  TargetState(conversation=GROUP, draft_text="正在打字…", observed=True, window_ready=True)]
        guard = _guard_sequence(sent, states)
        verdict = guard.send(GROUP, "好的，已记单")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, SendVerdict.DRAFT_PRESENT)
        self.assertEqual(sent, [], "二次核对发现草稿时不得发送")

    def test_clean_target_sends(self):
        sent: list[tuple[str, str]] = []
        guard = _guard(sent, TargetState(conversation=GROUP, observed=True, window_ready=True))
        self.assertTrue(guard.send(GROUP, "好的，已记单：苹果 2箱").ok)
        self.assertEqual(sent, [(GROUP, "好的，已记单：苹果 2箱")])

    def test_hold_reason_is_reported_to_the_interface(self):
        guard = _guard([], TargetState(conversation=GROUP, draft_text="x", observed=True, window_ready=True))
        guard.send(GROUP, "好的", order_id="o_1")
        self.assertEqual(guard.hold_summary()["o_1"], SendVerdict.DRAFT_PRESENT)

    def test_a_hold_is_not_reported_as_an_unknown_send(self):
        """没有往微信里打字，所以不是"结果未知"，不需要对账。"""
        from wechat_ai_assistant.agent import AgentStore

        directory = Path(tempfile.mkdtemp(prefix="pyw-hold-"))
        try:
            outbox = ReceiptOutbox(AgentStore(str(directory / "a.db")), sender=lambda t, x: None)
            outbox.enqueue(ReceiptRequest(order_id="o_1", order_version=1, conversation=GROUP, text="好的"))
            outcomes = outbox.dispatch(
                allowed_targets=(GROUP,),
                send=_raising_guard_hold(),
            )
            self.assertEqual(outcomes[0]["status"], ReceiptStatus.HELD)
            self.assertEqual(outbox.unknown_count(), 0, "暂缓不是结果未知")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def _guard(sent, state, expected_account: str = "", allow_real: bool = True):
    mark_real_sending_verified(allow_real)
    from wechat_ai_assistant.wechat_send import SafeReceiptSender

    guard = SafeReceiptSender(
        StaticInspector(state), inner=lambda t, x: sent.append((t, x)),
        expected_account=expected_account,
    )
    return guard


def _guard_sequence(sent, states):
    """按顺序返回状态，用来模拟"检查通过之后商户才开始打字"。"""
    from wechat_ai_assistant.wechat_send import SafeReceiptSender

    mark_real_sending_verified(True)
    remaining = list(states)

    class SequenceInspector:
        def inspect(self, conversation):
            return remaining.pop(0) if remaining else states[-1]

    return SafeReceiptSender(
        SequenceInspector(), inner=lambda t, x: sent.append((t, x))
    )


def _raising_guard_hold():
    def sender(target, text):
        raise SendHeld(_check(SendVerdict.DRAFT_PRESENT))

    return sender


def _check(reason: str):
    from wechat_ai_assistant.wechat_send import SendCheck

    return SendCheck(False, reason)


# ======================================================= 2. 短消息合并


class MergeSettingsTests(unittest.TestCase):
    def test_defaults_are_explicitly_not_a_conclusion(self):
        settings = MergeSettings()
        self.assertGreater(settings.silence_window, 0)
        self.assertGreaterEqual(settings.max_wait, settings.silence_window)
        self.assertTrue(settings.enabled)

    def test_max_wait_cannot_be_less_than_the_silence_window(self):
        """否则「最多等这么久」就是一句空话。"""
        settings = MergeSettings(silence_window=60, max_wait=10)
        self.assertEqual(settings.max_wait, 60)

    def test_non_positive_windows_are_rejected(self):
        with self.assertRaises(ValueError):
            MergeSettings(silence_window=0)
        with self.assertRaises(ValueError):
            MergeSettings(max_wait=-1)


class MergeBufferTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="pyw-merge-"))
        self.merger = MessageMerger(str(self.directory / "m.db"), MergeSettings(silence_window=10, max_wait=20))
        self.clock = [1000.0]
        self.merger._clock = lambda: self.clock[0]
        self.key = MergeKey(account_id="wxid", conversation=GROUP, sender="张姐")

    def tearDown(self):
        self.merger.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def offer(self, event_id, text, offset=0.0, key=None, quoted=""):
        return self.merger.offer(
            key or self.key,
            BufferedMessage(event_id=event_id, text=text, sent_at=1000.0 + offset, quoted=quoted),
        )

    def test_short_messages_from_one_sender_merge_into_one_batch(self):
        self.assertTrue(self.offer("e1", "苹果两箱", 0))
        self.assertTrue(self.offer("e2", "再加一箱", 3))
        self.assertTrue(self.offer("e3", "明天送", 8))
        self.clock[0] += 30
        batches = self.merger.flush()
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch.size, 3)
        self.assertEqual(batch.representative_event_id, "e1")
        self.assertIn("苹果两箱\n再加一箱\n明天送", batch.text)

    def test_different_senders_do_not_merge(self):
        """两个人同时在群里下单不该被合成一笔。"""
        self.offer("e1", "苹果两箱", 0)
        self.offer("e2", "橙子三箱", 1, key=MergeKey("wxid", GROUP, "王老板"))
        self.clock[0] += 30
        batches = self.merger.flush()
        self.assertEqual(len(batches), 2)
        self.assertEqual({b.key.sender for b in batches}, {"张姐", "王老板"})

    def test_different_conversations_do_not_merge(self):
        self.offer("e1", "苹果两箱", 0)
        self.offer("e2", "香蕉五箱", 1, key=MergeKey("wxid", "另一个群", "张姐"))
        self.clock[0] += 30
        self.assertEqual(len(self.merger.flush()), 2)

    def test_quote_reference_creates_a_separate_topic(self):
        """接龙场景：不同人引用同一条时各自成组，不会混成一单。"""
        self.offer("e1", "+1", 0, key=MergeKey("wxid", GROUP, "甲", topic="接龙1"))
        self.offer("e2", "+1", 1, key=MergeKey("wxid", GROUP, "乙", topic="接龙1"))
        self.offer("e3", "苹果两箱", 2, key=MergeKey("wxid", GROUP, "甲", topic="接龙2"))
        self.clock[0] += 30
        batches = self.merger.flush()
        self.assertEqual(len(batches), 3)

    def test_duplicate_delivery_is_ignored(self):
        self.assertTrue(self.offer("e1", "苹果两箱", 0))
        self.assertFalse(self.offer("e1", "苹果两箱", 0), "同一 event_id 不得进入两次")
        self.clock[0] += 30
        self.assertEqual(self.merger.flush()[0].size, 1)

    def test_silence_window_closes_the_batch(self):
        self.offer("e1", "苹果两箱", 0)
        self.clock[0] += 11
        self.assertEqual(len(self.merger.ready_groups()), 1)

    def test_max_wait_closes_even_while_messages_keep_arriving(self):
        """客户连发也不能一直等。"""
        for index in range(20):
            self.offer(f"e{index}", f"第{index}句", index * 2)
            self.clock[0] = 1000.0 + index * 2
        # 最后一条在第 38 秒；静默窗口 10 秒未到，但最大等待 20 秒已过。
        ready = self.merger.ready_groups()
        self.assertEqual(len(ready), 1, "超过最大等待后必须变为可结算")
        batch = self.merger.flush()[0]
        self.assertEqual(batch.size, 20, "到期时应一次性结算整组")

    def test_merge_can_be_disabled(self):
        self.merger.settings = MergeSettings(enabled=False)
        self.assertFalse(self.offer("e1", "苹果两箱", 0))

    def test_evidence_map_points_back_to_every_original(self):
        self.offer("e1", "苹果两箱", 0)
        self.offer("e2", "再加一箱", 3)
        self.clock[0] += 30
        batch = self.merger.flush()[0]
        mapping = self.merger.evidence_map(batch.representative_event_id)
        self.assertEqual(mapping["merged_event_ids"], ["e1", "e2"])
        self.assertEqual(mapping["role"], "representative")

    def test_only_the_representative_is_parsed(self):
        """原始消息不得再被解析一次 —— 那会重复计费和重复建单。"""
        self.offer("e1", "苹果两箱", 0)
        self.offer("e2", "再加一箱", 3)
        self.clock[0] += 30
        batch = self.merger.flush()[0]
        self.assertEqual(self.merger.representative_of("e1"), "e1")
        self.assertEqual(self.merger.representative_of("e2"), "e1")
        self.assertNotEqual(batch.representative_event_id, "e2")

    def test_restart_keeps_the_open_buffer(self):
        """重启不得把累积了一半的订单拆成两笔。"""
        self.offer("e1", "苹果两箱", 0)
        self.offer("e2", "再加一箱", 3)
        path = str(self.directory / "m.db")
        self.merger.close()
        self.merger = MessageMerger(path, MergeSettings(silence_window=10, max_wait=20))
        self.merger._clock = lambda: self.clock[0]
        self.assertEqual(len(self.merger.ready_groups()), 0, "刚收到的还在静默窗口内")
        self.clock[0] += 30
        batches = self.merger.flush()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].size, 2, "重启后应恢复成同一组")

    def test_late_message_is_flagged_not_silently_dropped(self):
        self.offer("e1", "苹果两箱", 0)
        self.clock[0] += 30
        self.merger.flush()
        self.offer("e2", "补充：不要鸡蛋", 5, quoted="")
        self.merger.mark_late("e2")
        self.clock[0] += 60
        batches = self.merger.flush()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].late_count, 1)
        mapping = self.merger.evidence_map("e2")
        self.assertIn("e2", mapping["late_event_ids"])


# ======================================================= 3. 统计口径


class MetricDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-metric-"))
        self.store = OrderStore(str(self.dir / "o.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", GROUP)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.builder = DashboardBuilder(self.store, self.tenant, clock=lambda: NOW)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _order(self, sku="APL", quantity=2, status="draft", customer=None, version=1, history=None,
               change_type="new"):
        oid = "o_" + uid()
        message_id = "msg_" + uid()
        self.store.db.execute(
            "INSERT INTO imports(id,merchant_id,group_id,source,status,created_at) VALUES(?,?,?,?,?,?)",
            ("i_" + uid(), self.tenant, GROUP, "t-" + uid(), "complete", NOW.isoformat()),
        )
        self.store.db.execute(
            "INSERT INTO messages(id,merchant_id,import_id,group_id,sender,customer_id,content,kind,"
            "sent_at,sent_at_epoch,observed_at,time_precision,source_id,match_confidence)"
            " VALUES(?,?,(SELECT id FROM imports WHERE merchant_id=? ORDER BY created_at DESC LIMIT 1),?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, self.tenant, self.tenant, GROUP, "张姐", customer or self.customer,
             "苹果两箱", "text", NOW.isoformat(), NOW.timestamp(), NOW.isoformat(), "exact",
             "src-" + uid(), 0.95),
        )
        self.store.db.execute(
            "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
            "created_at,updated_at,version,change_type,history_order_id,business_at_epoch)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, self.tenant, customer or self.customer, GROUP, status, "2026-09-26", None,
             NOW.isoformat(), NOW.isoformat(), version, change_type, history, NOW.timestamp()),
        )
        product = self.store.db.execute(
            "SELECT id,price_cents,unit FROM products WHERE merchant_id=? AND sku=?", (self.tenant, sku)
        ).fetchone()
        self.store.db.execute(
            "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) VALUES(?,?,?,?,?,?)",
            ("i_" + uid(), oid, product["id"], quantity, product["unit"], product["price_cents"]),
        )
        self.store.db.execute("INSERT INTO evidence(id,merchant_id,order_id,message_id,role) VALUES(?,?,?,?,'source')",
                              ("e_" + uid(), self.tenant, oid, message_id))
        self.store.db.execute("INSERT INTO review_tasks(id,merchant_id,order_id,reason,status) VALUES(?,?,?,?,'open')",
                              ("r_" + uid(), self.tenant, oid, "ambiguous_append"))
        self.store.db.commit()
        return oid

    def test_every_metric_has_a_written_definition(self):
        for key in ("effective_orders", "actionable", "stock", "payment_claims", "send_errors", "quota"):
            self.assertIn(key, METRIC_DEFINITIONS)
            self.assertTrue(METRIC_DEFINITIONS[key].strip())

    def test_a_change_does_not_double_count_in_stock(self):
        """改一次量后，旧版本被替代，备货不能变成 2+3=5 箱。"""
        first = self._order(quantity=2, status="review")
        self._order(quantity=3, status="review", history=first)
        self.store.db.execute("UPDATE orders SET status='superseded' WHERE id=?", (first,))
        self.store.db.commit()
        stock = {row.sku: row for row in self.builder.stock_summary()}
        self.assertEqual(stock["APL"].quantity, 3, "被替代的版本不得计入备货")

    def test_a_pending_change_version_suppresses_its_predecessor(self):
        """真实模型跑出来的 bug：追加产生新版本后，确认前两个版本都算「有效」，
        备货变成 2+3=5 箱。商户照着采购会多买两箱。

        这里复现那条真实路径：新版本处于 review（待核对），旧版本还是 draft。
        ``change_type`` 必须是 ``modify`` —— 真实追加/改量就是这么写的。
        ``new`` 意味着"另一笔生意照着老单建"，那**不该**压掉被参照的那一单
        （见 :meth:`test_a_derived_order_does_not_hide_its_reference`）。
        """
        first = self._order(quantity=2, status="draft")
        self._order(quantity=3, status="review", history=first, change_type="modify")
        stock = {row.sku: row for row in self.builder.stock_summary()}
        self.assertEqual(stock["APL"].quantity, 3, "待核对的新版本顶替旧版本后只能算一次")
        effective = self.builder.effective_orders()
        self.assertEqual(len(effective), 1, "同一笔订单只应有一个有效版本")

    def test_a_derived_order_does_not_hide_its_reference(self):
        """「老样子」生成的新单**不是**被参照那一单的新版本。

        两者都该进备货：老单 3 箱还没取消，新单又要 3 箱。只显示 3 会让商户
        少买 3 箱。
        """
        history = self._order(quantity=3, status="confirmed")
        self._order(quantity=3, status="draft", history=history, change_type="new")
        stock = {row.sku: row for row in self.builder.stock_summary()}
        self.assertEqual(stock["APL"].quantity, 6.0, "两笔独立生意都要备货")

    def test_superseding_on_confirm_still_works(self):
        """排除只是不重复计入；确认后旧版本仍应被真正标记为 superseded。"""
        first = self._order(quantity=2, status="draft")
        second = self._order(quantity=3, status="review", history=first, change_type="modify")
        self.store.confirm(self.tenant, second, "u1")
        old = self.store.order_detail(self.tenant, first)
        self.assertEqual(old["status"], "superseded")

    def test_auto_recorded_orders_are_not_actionable(self):
        self._order(status="draft")
        self.assertEqual(self.builder.actionable_orders(), [], "自动记单不是待办")
        self.assertEqual(self.builder.auto_recorded_count(), 1)

    def test_review_orders_are_actionable(self):
        self._order(status="review")
        self.assertEqual(len(self.builder.actionable_orders()), 1)
        self.assertEqual(self.builder.exception_count(), 1)

    def test_scope_excludes_older_orders(self):
        old = self._order()
        self.store.db.execute(
            "UPDATE orders SET updated_at=? WHERE id=?", ((NOW - timedelta(days=5)).isoformat(), old)
        )
        self.store.db.commit()
        self.assertEqual(self.builder.effective_orders(), [], "统计范围之外的不算")
        self.assertIn("1 天", self.builder.scope_text())
        self.assertIn("1 小时", DashboardBuilder(self.store, self.tenant, scope_hours=1).scope_text())
        self.assertIn("3 天", DashboardBuilder(self.store, self.tenant, scope_hours=72).scope_text())

    def test_effective_and_actionable_statuses_are_disjoint_sets(self):
        """自动记单不作为待办：它已经在 EFFECTIVE 里，不在 ACTIONABLE 里。"""
        self.assertNotIn("superseded", EFFECTIVE_STATUSES)
        self.assertNotIn("cancelled", EFFECTIVE_STATUSES)
        self.assertIn("confirmed", EFFECTIVE_STATUSES)
        self.assertNotIn("draft", ACTIONABLE_STATUSES, "自动记单不是待办")
        self.assertIn("review", ACTIONABLE_STATUSES)
        self.assertIn("cancel_review", ACTIONABLE_STATUSES)


# ======================================================= 4. 限流与恢复


class HeldReceiptRevalidationTests(unittest.TestCase):
    """恢复发送时必须逐条重新核对，不能一次性全放。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-held-"))
        self.store = OrderStore(str(self.dir / "o.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", GROUP)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_app(self):
        app = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant,
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=SimulatedConversations([(GROUP, ConversationKind.GROUP, 128)]),
            history_reader_fn=SimulatedHistory(default_count=0),
            simulated=True,
        )
        app.attach(object(), _Client(), _Parse(), backend_is_model=False)
        app.choose_live_conversation(app.refresh_conversations()[0])
        self.apps.append(app)
        return app

    def _queue(self, app, order_id, version, conversation=GROUP):
        app.outbox.enqueue(
            ReceiptRequest(order_id=order_id, order_version=version, conversation=conversation, text="好的，已记单")
        )
        # 立刻消费掉租约：模拟"发送前被暂停"
        for lease in app.agent_store.claim_outbox_leases(limit=50, lease_seconds=30):
            app.agent_store.mark_hold(lease.command.command_id, "paused")
            app.agent_store.release_lease(lease.command.command_id, lease.token)

    def test_resuming_releases_only_still_valid_receipts(self):
        app = self.make_app()
        self._queue(app, "o_new", 3)
        self._queue(app, "o_stale", 1)
        app.set_mode(RunMode.AUTO_TAKE)
        app.set_allow_local_automation(True)
        app._latest_versions = lambda: {GROUP: 3}
        released, re_held = app._release_held_validated()
        self.assertEqual(released, 1, "只放行版本最新的那条")
        self.assertEqual(re_held, 1, "旧版本必须继续暂缓")
        reasons = app.outbox.held_reasons()
        self.assertIn("stale_order_version", reasons.values())

    def test_resuming_in_the_wrong_mode_keeps_everything_held(self):
        app = self.make_app()
        self._queue(app, "o_1", 1)
        app.set_mode(RunMode.BACKGROUND)   # 记单但不发
        released, re_held = app._release_held_validated()
        self.assertEqual(released, 0, "后台整理模式不得发送")
        self.assertEqual(re_held, 1)
        self.assertIn("mode_or_switch_disabled", app.outbox.held_reasons().values())

    def test_receipts_for_another_conversation_stay_held(self):
        app = self.make_app()
        self._queue(app, "o_1", 1, conversation="别的群")
        app.set_mode(RunMode.AUTO_TAKE)
        app.set_allow_local_automation(True)
        released, re_held = app._release_held_validated()
        self.assertEqual(released, 0)
        self.assertIn("conversation_not_allowed", app.outbox.held_reasons().values())


class DrainBoundTests(unittest.TestCase):
    """413 的 drain 必须在时间和大小上都封顶。"""

    def test_drain_has_a_time_limit(self):
        from wechat_ai_assistant import gateway_server

        self.assertGreater(gateway_server.DRAIN_TIMEOUT_SECONDS, 0)
        self.assertLessEqual(gateway_server.DRAIN_TIMEOUT_SECONDS, 30.0,
                             "drain 不得无限等待，否则一个慢速请求能占住工作线程")

    def test_drain_has_a_size_limit(self):
        from wechat_ai_assistant import gateway_server

        self.assertGreater(gateway_server.DRAIN_LIMIT_BYTES, 0)

    def test_slow_body_does_not_hang_the_handler(self):
        """声明 50MB 却一个字节都不发的客户端，不能把处理线程钉住。"""
        import shutil as _shutil
        import socket
        import tempfile as _tempfile
        import threading as _threading
        from http.server import ThreadingHTTPServer

        from wechat_ai_assistant.gateway import ProductGateway
        from wechat_ai_assistant.gateway_server import GatewayHTTPRequestHandler
        from wechat_ai_assistant.product import ProductStore

        directory = Path(_tempfile.mkdtemp(prefix="pyw-drain-"))
        gateway = ProductGateway(ProductStore(str(directory / "p.db")))

        class Handler(GatewayHTTPRequestHandler):
            pass

        Handler.gateway = gateway

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

        server = Quiet(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = _threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(
                    b"POST /v1/order-parse-jobs HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 50000000\r\n\r\n"
                )
                # 只发头部，一个字节的数据体都不发
                client.settimeout(25)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = client.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                except socket.timeout:
                    self.fail("客户端在 25 秒内没有收到任何响应")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 15.0, "慢速大请求不得拖住处理线程")
            self.assertIn(b"413", data, "仍应返回 413")
        finally:
            server.shutdown()
            server.server_close()
            gateway.store.close()
            _shutil.rmtree(directory, ignore_errors=True)


class NoDoubleChargeTests(unittest.TestCase):
    def test_unknown_outcome_never_re_executes(self):
        from business_agent import AgentCore, FakeBackend, RunStatus
        from business_agent.contracts.errors import OutcomeUnknown
        from business_agent.contracts.request import AgentRequest, AsOf, Budget, Event, Policy, Scope
        from order_agent import OrderPlugin

        calls: list[int] = []

        class Counting(FakeBackend):
            def run(self, prompt, request):
                calls.append(1)
                raise OutcomeUnknown("断了")

        request = AgentRequest(
            request_id="r1", idempotency_key="i1", task_type="order_parse",
            scope=Scope(tenant_id="t"),
            events=(Event(event_id="e1", body="苹果两箱", occurred_at=NOW),),
            as_of=AsOf(instant=NOW), policy=Policy(name="p"), budget=Budget(timeout_seconds=5),
        )
        core = AgentCore(backend=Counting(), plugin=OrderPlugin())
        result = core.run(request)
        self.assertEqual(result.status, RunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(len(calls), 1, "结果未知不得重复执行，否则会重复计费")
        # 。Usage.cost_units。是 business_agent 包自己的字段（报告平台实际消耗，
        # 包注释写明 "The host decides what to charge"），**不是**商户账单。
        # 把这里改名成商户口径会把「平台花了多少」和「商户被扣多少」混成一件事。
        self.assertEqual(result.usage.cost_units, 0, "结果未知不产生客户账单")

    def test_timeout_does_not_retry_either(self):
        from business_agent import AgentCore, FakeBackend, RunStatus
        from business_agent.contracts.errors import AgentError, ErrorCategory
        from business_agent.contracts.request import AgentRequest, AsOf, Budget, Event, Policy, Scope
        from order_agent import OrderPlugin

        calls: list[int] = []

        class TimingOut(FakeBackend):
            def run(self, prompt, request):
                calls.append(1)
                raise AgentError(ErrorCategory.TIMEOUT, "超时")

        request = AgentRequest(
            request_id="r1", idempotency_key="i1", task_type="order_parse",
            scope=Scope(tenant_id="t"),
            events=(Event(event_id="e1", body="苹果两箱", occurred_at=NOW),),
            as_of=AsOf(instant=NOW), policy=Policy(name="p"), budget=Budget(timeout_seconds=5),
        )
        result = AgentCore(backend=TimingOut(), plugin=OrderPlugin()).run(request)
        self.assertEqual(result.status, RunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(len(calls), 1)


class MessageKindNormalisationTests(unittest.TestCase):
    """微信导出的消息类型是中文。之前中文「文本」被当成不支持的媒体，
    导致**每一条真实文字消息**都进人工核对，永远不自动记单。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-kind-"))
        self.store = OrderStore(str(self.dir / "o.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_chinese_text_kind_is_parsable(self):
        from wechat_ai_assistant.orders import normalize_message_kind

        self.assertEqual(normalize_message_kind("文本"), "text")
        self.assertEqual(normalize_message_kind("文字"), "text")
        self.assertEqual(normalize_message_kind("text"), "text")
        self.assertEqual(normalize_message_kind("图片"), "image")
        self.assertEqual(normalize_message_kind("语音"), "voice")
        self.assertEqual(normalize_message_kind(""), "text")

    def test_wechat_shaped_messages_produce_real_orders(self):
        """按微信导出的原样字段导入，必须真的建出订单。"""
        from datetime import timedelta

        self.store.add_import(
            self.tenant, self.group,
            [
                {"消息发送人": "张姐", "消息内容": "苹果两箱", "消息类型": "文本",
                 "消息发送时间": iso(NOW - timedelta(hours=2)), "source_id": "k1"},
                {"消息发送人": "张姐", "消息内容": "苹果三箱", "消息类型": "文本",
                 "消息发送时间": iso(NOW - timedelta(hours=1)), "source_id": "k2"},
            ],
            source="wechat-export-1", source_type="wechat_history",
        )
        parsed = self.store.parse_import(self.tenant, self.group, None)
        self.assertEqual(parsed["drafts"], 2, "两条都在目录里的文字消息都应自动记单")
        self.assertEqual(parsed["review"], 0, "不该有消息被判为不支持的媒体")
        orders = self.store.list_orders(self.tenant)
        self.assertEqual(len(orders), 2)
        detail = self.store.order_detail(self.tenant, orders[0]["id"])
        self.assertTrue(detail["items"], "建出来的订单必须有商品明细")

    def test_real_media_still_goes_to_review(self):
        from datetime import timedelta

        self.store.add_import(
            self.tenant, self.group,
            [{"消息发送人": "张姐", "消息内容": "[图片]", "消息类型": "图片",
              "消息发送时间": iso(NOW - timedelta(hours=1)), "source_id": "m1"}],
            source="wechat-export-2", source_type="wechat_history",
        )
        parsed = self.store.parse_import(self.tenant, self.group, None)
        self.assertEqual(parsed["drafts"], 0)
        reasons = {row["reason"] for row in self.store.db.execute(
            "SELECT reason FROM review_tasks WHERE merchant_id=?", (self.tenant,)
        )}
        self.assertIn("unsupported_media:image", reasons, "图片仍应转人工，且理由正确")


def iso(moment: datetime) -> str:
    return moment.isoformat()


class _Client:
    def account(self):
        return {"subscription": {"active": True, "remaining_micro_usd": 99,
                                 "plan_code": "标准版", "period_end": "2026-10-25T00:00:00+00:00"}}


class _Parse:
    model_executed = False

    def submit(self, message_id, on_progress=None):
        return ParseProgress("done", "已处理", message_id=message_id, job_id="job")

    def settle(self, on_progress=None):
        return ParseProgress("pending", "没有待处理的解析")


if __name__ == "__main__":
    unittest.main()
