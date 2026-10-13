"""端到端场景测试：不依赖 Tkinter，但走真实的产品路径。

这些测试证明的是**软件逻辑**：向导状态机、闭环服务、回执幂等、额度恢复、
暂停与版本核对。它们不等于真实微信、真实 OpenCode 或真实模型验证。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.application import AppEvents, MerchantApplication  # noqa: E402
from pyweixin.conversations import Conversation, ConversationKind, SimulatedConversations  # noqa: E402
from pyweixin.history_read import HistoryRange, SimulatedHistory, TimeWindow  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.receipts import ReceiptStatus, build_receipt_text  # noqa: E402
from pyweixin.runmode import RunMode  # noqa: E402
from pyweixin.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402
from pyweixin.wizard import QuotaBlocked  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


class FakeClient:
    """模拟产品账号。额度可调，用于测不足与恢复。"""

    def __init__(self, remaining: int = 100, active: bool = True, error: Exception | None = None):
        self.remaining = remaining
        self.active = active
        self.error = error
        self.submitted: list[tuple] = []

    def account(self) -> dict:
        if self.error is not None:
            raise self.error
        return {
            "subscription": {
                "active": self.active,
                "remaining_micro_usd": self.remaining,
                "plan_code": "标准版",
                "period_end": "2026-10-25T00:00:00+00:00",
            }
        }


class Harness(unittest.TestCase):
    """构造一个完整可用、但不需要微信/网络的应用实例。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-e2e-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.store.add_product(self.tenant, "EGG", "鸡蛋", "箱", 3400, ["鸡蛋"])
        self.sent: list[tuple[str, str]] = []
        self.client = FakeClient()
        self.app: MerchantApplication | None = None

    def tearDown(self):
        if self.app is not None:
            self.app.close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add_message(self, content: str, moment: datetime, source_id: str, sender: str = "张姐"):
        self.store.add_import(
            self.tenant,
            self.group,
            [{"sender": sender, "content": content, "sent_at": iso(moment), "source_id": source_id, "stable_source_id": True}],
            source=f"import-{source_id}",
            source_type="test",
        )
        row = self.store.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? AND source_id=?", (self.tenant, source_id)
        ).fetchone()
        return dict(row)

    def make_app(self, **kwargs) -> MerchantApplication:
        self.app = MerchantApplication(
            data_dir=self.dir,
            orders=self.store,
            tenant=self.tenant,
            probe=kwargs.pop("probe", SimulatedWeChat([WeChatState.LOGGED_IN])),
            launcher=kwargs.pop("launcher", None),
            conversation_source=kwargs.pop("conversation_source", SimulatedConversations([(self.group, ConversationKind.GROUP, 128)])),
            history_reader_fn=kwargs.pop("history_reader_fn", SimulatedHistory(default_count=0)),
            gateway_url=kwargs.pop("gateway_url", "http://gateway.test"),
            client_factory=kwargs.pop("client_factory", None),
            sender=kwargs.pop("sender", lambda target, text: self.sent.append((target, text))),
            events=kwargs.pop("events", AppEvents()),
            simulated=kwargs.pop("simulated", True),
            # 这里测的是流程与文案，不是认人：群读取固定走成员名单，
            # 否则默认的头像模式会去点真微信窗口。
            sender_source="members",
        )
        return self.app


# --------------------------------------------------------------- ① 微信


class WeChatDetectionTests(Harness):
    def test_each_state_is_distinguished(self):
        for state in (
            WeChatState.NOT_INSTALLED,
            WeChatState.NOT_STARTED,
            WeChatState.WAITING_SCAN,
            WeChatState.WAITING_CONFIRM,
            WeChatState.LOGGED_IN,
            WeChatState.OFFLINE,
            WeChatState.UNREADABLE,
        ):
            app = self.make_app(probe=SimulatedWeChat([state]))
            self.assertEqual(app.check_wechat().state, state, state)
            app.close()
            self.app = None

    def test_not_started_offers_a_launch_button(self):
        sim = SimulatedWeChat([WeChatState.NOT_STARTED, WeChatState.WAITING_SCAN, WeChatState.LOGGED_IN])
        app = self.make_app(probe=sim, launcher=sim)
        status = app.check_wechat()
        self.assertEqual(status.state, WeChatState.NOT_STARTED)
        self.assertEqual(status.copy()[2], "打开微信", "未启动时必须有可点的按钮")
        self.assertTrue(app.start_wechat())
        self.assertEqual(sim.launches, 1)

    def test_login_completes_advances_to_conversations(self):
        from types import SimpleNamespace

        sim = SimulatedWeChat([WeChatState.NOT_STARTED, WeChatState.WAITING_SCAN, WeChatState.LOGGED_IN])
        app = self.make_app(probe=sim, launcher=sim)
        app.session = SimpleNamespace(merchant_name="测试商家")
        steps: list[str] = []
        app.wizard.on_step = steps.append
        app.check_wechat()
        app.start_wechat()
        sim.advance()
        app.check_wechat()   # 等待扫码
        sim.advance()
        app.check_wechat()   # 已登录
        self.assertEqual(app.wizard.step, "conversations")
        self.assertIn("conversations", steps)

    def test_already_logged_in_does_not_ask_for_rescan(self):
        from types import SimpleNamespace

        app = self.make_app(probe=SimulatedWeChat([WeChatState.LOGGED_IN]))
        app.session = SimpleNamespace(merchant_name="测试商家")
        status = app.check_wechat()
        self.assertTrue(status.ready)
        self.assertEqual(app.wizard.step, "conversations")
        self.assertNotIn("打开微信", status.copy()[2])

    def test_process_existence_is_not_treated_as_logged_in(self):
        """进程在但停在扫码页 ≠ 已登录。"""
        sim = SimulatedWeChat([WeChatState.WAITING_SCAN])
        app = self.make_app(probe=sim)
        status = app.check_wechat()
        self.assertFalse(status.ready)
        self.assertEqual(app.wizard.step, "wechat")

    def test_product_gate_blocks_advance_until_logged_in(self):
        """微信就绪、但产品没登录时，停在第 1 步不放行。"""
        from types import SimpleNamespace

        app = self.make_app(probe=SimulatedWeChat([WeChatState.LOGGED_IN]))
        self.assertIsNone(app.session)
        app.check_wechat()
        self.assertEqual(app.wizard.step, "wechat")
        app.session = SimpleNamespace(merchant_name="测试商家")
        app.check_wechat()
        self.assertEqual(app.wizard.step, "conversations")

    def test_simulated_results_are_flagged(self):
        app = self.make_app()
        self.assertTrue(app.check_wechat().simulated)


# --------------------------------------------------------------- ② 会话


class ConversationTests(Harness):
    def test_conversations_are_discovered_with_disambiguation(self):
        source = SimulatedConversations(
            [("鲜果群", ConversationKind.GROUP, 128), ("鲜果群", ConversationKind.GROUP, 256)]
        )
        app = self.make_app(conversation_source=source)
        found = app.refresh_conversations()
        names = [item.disambiguation for item in found]
        self.assertIn("鲜果群（128人）", names)
        self.assertIn("鲜果群（256人）", names)

    def test_same_name_conversations_have_distinct_identities(self):
        source = SimulatedConversations(
            [("鲜果群", ConversationKind.GROUP, 128), ("鲜果群", ConversationKind.GROUP, 256)]
        )
        found = source.list_conversations()
        self.assertNotEqual(found[0].identity, found[1].identity)

    def test_wrong_conversation_fails_the_execution_check(self):
        source = SimulatedConversations(
            [("鲜果群", ConversationKind.GROUP, 128), ("鲜果群", ConversationKind.GROUP, 256)]
        )
        found = source.list_conversations()
        self.assertFalse(found[0].matches(found[1]))

    def test_only_one_live_conversation_is_allowed(self):
        app = self.make_app(conversation_source=SimulatedConversations(
            [("群A", ConversationKind.GROUP, 10), ("群B", ConversationKind.GROUP, 20)]
        ))
        found = app.refresh_conversations()
        app.choose_live_conversation(found[0])
        app.choose_live_conversation(found[1])
        self.assertEqual(app.live_conversation().name, "群B", "后一次选择覆盖前一次")

    def test_history_can_select_several_conversations(self):
        app = self.make_app(conversation_source=SimulatedConversations(
            [("群A", ConversationKind.GROUP, 10), ("群B", ConversationKind.GROUP, 20)]
        ))
        found = app.refresh_conversations()
        app.choose_history_conversations(found)
        self.assertEqual(len(app.wizard.conversations.history_selected), 2)


# --------------------------------------------------------------- ③ 历史


class HistoryTests(Harness):
    def _read(self, app, **kwargs):
        return app.wizard.read_history(**kwargs)

    def test_actual_coverage_is_reported_honestly(self):
        """请求 7 天但只读到 3 天，界面必须显示 3 天。"""
        history = SimulatedHistory(
            default_count=10,
        )
        app = self.make_app(history_reader_fn=history)
        app.wizard.pipeline = type("P", (), {"orders": self.store})()
        app.wizard.orders = lambda: self.store
        found = app.refresh_conversations()
        app.choose_history_conversations(found[:1])
        result = self._read(app, range_kind=HistoryRange.LAST_WEEK, max_messages=10)
        self.assertIn("实际读到", result.actual_range_text())
        self.assertIn("您选择的范围是", result.requested_range_text())
        self.assertNotEqual(
            result.actual_range_text().split("时间范围")[1].strip(" 至").split()[0],
            "最近7天",
        )

    def test_progress_is_reported_per_conversation(self):
        app = self.make_app(history_reader_fn=SimulatedHistory(default_count=3))
        app.wizard.orders = lambda: self.store
        app.wizard.pipeline = type("P", (), {"orders": self.store})()
        found = app.refresh_conversations()
        app.choose_history_conversations(found)
        seen: list[int] = []
        self._read(app, range_kind=HistoryRange.LAST_DAY, on_progress=lambda p: seen.append(p.done))
        self.assertEqual(seen[-1], len(found))
        self.assertTrue(all(0 <= value <= len(found) for value in seen))

    def test_cancel_stops_reading_and_keeps_partial(self):
        slow = SimulatedHistory(default_count=2, delay=0.05)
        app = self.make_app(history_reader_fn=slow)
        app.wizard.orders = lambda: self.store
        app.wizard.pipeline = type("P", (), {"orders": self.store})()
        found = app.refresh_conversations()
        app.choose_history_conversations(found)

        def cancel_soon(progress):
            if progress.done >= 0:
                app.cancel_history()

        result = self._read(app, range_kind=HistoryRange.LAST_DAY, on_progress=cancel_soon)
        self.assertTrue(result.cancelled, "取消后必须标记为已取消")
        self.assertLess(len(result.coverage), len(found), "取消后不应继续读后面的会话")

    def test_resume_skips_exhausted_conversations(self):
        """只有真正读完（没撞到条数上限）才标记完成并跳过重读。

        撞到上限时不算读完 —— 微信可能还有更早的历史，必须如实标记。
        """
        app = self.make_app(history_reader_fn=SimulatedHistory(default_count=2))
        app.wizard.orders = lambda: self.store
        app.wizard.pipeline = type("P", (), {"orders": self.store})()
        found = app.refresh_conversations()
        app.choose_history_conversations(found[:1])
        first = self._read(app, range_kind=HistoryRange.LAST_DAY, max_messages=50)
        self.assertFalse(first.coverage[0].skipped)
        self.assertFalse(first.coverage[0].truncated, "没撞上限就不该说可能还有更早历史")
        second = self._read(app, range_kind=HistoryRange.LAST_DAY, max_messages=50, resume=True)
        self.assertTrue(second.coverage[0].skipped, "已读完的会话再次读取应跳过")

    def test_hitting_the_cap_is_reported_as_possibly_incomplete(self):
        app = self.make_app(history_reader_fn=SimulatedHistory(default_count=20))
        app.wizard.orders = lambda: self.store
        app.wizard.pipeline = type("P", (), {"orders": self.store})()
        found = app.refresh_conversations()
        app.choose_history_conversations(found[:1])
        result = self._read(app, range_kind=HistoryRange.LAST_DAY, max_messages=5)
        self.assertTrue(result.coverage[0].truncated)
        self.assertTrue(any("更早" in gap for gap in result.gaps), "撞上限必须提示可能不完整")

    def test_read_failure_is_reported_not_swallowed(self):
        def failing(**kwargs):
            raise RuntimeError("界面读不到")

        app = self.make_app(history_reader_fn=failing)
        app.wizard.orders = lambda: self.store
        app.wizard.pipeline = type("P", (), {"orders": self.store})()
        found = app.refresh_conversations()
        app.choose_history_conversations(found[:1])
        result = self._read(app, range_kind=HistoryRange.LAST_DAY)
        self.assertIn("界面读不到", result.coverage[0].error)
        self.assertTrue(result.gaps)

    def test_history_import_lands_in_the_existing_order_store(self):
        app = self.make_app(history_reader_fn=SimulatedHistory(default_count=4))
        app.wizard.orders = lambda: self.store
        app.wizard.pipeline = type("P", (), {"orders": self.store})()
        found = app.refresh_conversations()
        app.choose_history_conversations(found[:1])
        result = self._read(app, range_kind=HistoryRange.LAST_DAY)
        row = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE merchant_id=?", (self.tenant,)
        ).fetchone()
        # 默认读取窗口排除窗口外的消息，条数可以少于读取条数；关键是落进了现有库。
        self.assertGreater(int(row["c"]), 0)
        self.assertLessEqual(int(row["c"]), 4)
        self.assertEqual(result.imported, int(row["c"]))
        self.assertTrue(result.import_ids, "导入必须落在现有订单库并返回批次号")


# --------------------------------------------------------------- ④ 额度


class QuotaTests(Harness):
    def test_quota_headline_is_plain_chinese(self):
        app = self.make_app()
        app.attach(session=object(), client=self.client, parse_service=object())
        view = app.check_quota()
        # 额度现在是「进度条 + 百分比」，不再显示抽象的剩余数字。
        # 分母未知时**明说不限**，而不是画一个假的百分比。
        self.assertTrue("%" in view.headline() or "不限" in view.headline(),
                        f"既没给百分比也没说不限：{view.headline()}")
        for term in ("token", "api", "key", "session"):
            self.assertNotIn(term, view.headline().lower())

    def test_the_headline_is_not_wrapped_in_its_own_label(self):
        """额度行已经自带前缀，状态行不得再拼一次。

        以前显示成「剩余分析额度：剩余分析额度：200」。
        标签重复是商户一眼能看出的界面错误。

        前缀文字随着产品调整变过（现在是「分析额度已用 N%」），
        所以这里验的是「不重复」这件事，不是具体哪个词。
        """
        app = self.make_app()
        app.attach(session=object(), client=self.client, parse_service=object())
        headline = app.check_quota().headline()
        label = "".join(t for t in ("分析额度",) if t in headline)
        self.assertTrue(label, f"额度行应该说账度，实际是：{headline}")
        self.assertEqual(headline.count(label), 1, f"前缀重复了：{headline}")

    def test_the_wechat_status_line_is_not_prefixed_twice(self):
        app = self.make_app()
        view = app.refresh_home()
        line = view.status_line()
        self.assertNotIn("：微信", line, f"状态行标签重复：{line}")

    def test_exhausted_quota_blocks_but_keeps_read_data(self):
        self.add_message("苹果两箱", NOW - timedelta(hours=2), "q1")
        before = self.store.db.execute("SELECT COUNT(*) AS c FROM messages WHERE merchant_id=?", (self.tenant,)).fetchone()
        app = self.make_app()
        app.attach(session=object(), client=FakeClient(remaining=0), parse_service=object())
        self.assertFalse(app.check_quota().sufficient)
        with self.assertRaises(QuotaBlocked):
            app.wizard.analyze()
        after = self.store.db.execute("SELECT COUNT(*) AS c FROM messages WHERE merchant_id=?", (self.tenant,)).fetchone()
        self.assertEqual(int(before["c"]), int(after["c"]), "额度不足不得删除已读取的数据")

    def test_quota_recovery_continues_without_re_reading(self):
        self.add_message("苹果两箱", NOW - timedelta(hours=2), "q2")
        app = self.make_app()
        client = FakeClient(remaining=0)
        app.attach(session=object(), client=client, parse_service=object())
        app.check_quota()
        with self.assertRaises(QuotaBlocked):
            app.wizard.analyze()
        messages_before = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE merchant_id=?", (self.tenant,)
        ).fetchone()["c"]
        # 恢复额度
        client.remaining = 50
        view = app.check_quota()
        self.assertTrue(view.sufficient)
        messages_after = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE merchant_id=?", (self.tenant,)
        ).fetchone()["c"]
        self.assertEqual(messages_before, messages_after, "恢复额度后不重复读取")

    def test_no_real_payment_means_manual_activation_only(self):
        app = self.make_app()
        app.attach(session=object(), client=FakeClient(), parse_service=object())
        view = app.check_quota()
        self.assertEqual(view.purchase_method, "manual")
        self.assertIn("联系客服", view.purchase_hint)

    def test_expired_subscription_and_login_expiry_read_differently(self):
        app = self.make_app()
        app.attach(session=object(), client=FakeClient(active=False), parse_service=object())
        self.assertEqual(app.check_quota().headline(), "套餐已到期")
        app2 = self.make_app()
        from pyweixin.client_login import GatewayError

        app2.attach(session=object(), client=FakeClient(error=GatewayError("unauthenticated", "登录过期")), parse_service=object())
        self.assertIn("重新登录", app2.check_quota().message)
        app3 = self.make_app()
        app3.attach(session=object(), client=FakeClient(error=GatewayError("network_unavailable", "断了")), parse_service=object())
        self.assertIn("网络暂不可用", app3.check_quota().message)


# ------------------------------------------------------------- ⑤ 整理


class AnalyzeTests(Harness):
    def _stub_parse(self, outcome_by_message: dict[str, dict], record_orders: bool = True):
        """一个可控的解析服务。模拟网关任务 + 插件 + 规则校验这条路径。"""
        from pyweixin.local_parse import ParseProgress

        service = type("StubParse", (), {})()
        service.submitted = []
        service.model_executed = True

        def submit(message_id, on_progress=None):
            service.submitted.append(message_id)
            if on_progress:
                on_progress(ParseProgress("running", "解析处理中", message_id=message_id, job_id=f"job_{message_id[-6:]}"))
            return ParseProgress("done", "已生成待确认订单", message_id=message_id, job_id=f"job_{message_id[-6:]}")

        service.submit = submit

        def settle(on_progress=None):
            if not service.submitted:
                return ParseProgress("pending", "没有待处理的解析")
            message_id = service.submitted[-1]
            payload = outcome_by_message.get(message_id)
            if payload is None:
                return ParseProgress("done", "无需处理", message_id=message_id, order_id="")
            return ParseProgress(
                "done", "已生成待确认订单", message_id=message_id,
                job_id=f"job_{message_id[-6:]}", order_id=payload["order_id"],
            )

        service.settle = settle
        return service

    def test_normal_order_is_recorded_without_asking_the_merchant(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=3), "a1")
        order_id = self._create_order(message, "APL", 2)
        app = self.make_app()
        app.attach(session=object(), client=self.client, parse_service=self._stub_parse({message["id"]: {"order_id": order_id}}))
        found = app.refresh_conversations()
        app.choose_live_conversation(found[0])
        outcome = app.pipeline.process(message["id"])
        self.assertEqual(outcome.outcome, "done")
        self.assertEqual(outcome.order_id, order_id)
        self.assertFalse(outcome.needs_review, "正常订单不应要求逐单确认")

    def _create_order(self, message, sku, quantity, status="draft"):
        oid = "o_" + hashlib_hex()
        self.store.db.execute(
            "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,created_at,updated_at,version,change_type,business_at_epoch)"
            " VALUES(?,?,?,?,?,?,?,?,?,1,'new',?)",
            (oid, self.tenant, self.customer, self.group, status, "2026-09-26", message["import_id"], iso(NOW), iso(NOW), message["sent_at_epoch"]),
        )
        product = self.store.db.execute("SELECT id,price_cents,unit FROM products WHERE merchant_id=? AND sku=?", (self.tenant, sku)).fetchone()
        self.store.db.execute(
            "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) VALUES(?,?,?,?,?,?)",
            ("i_" + hashlib_hex(), oid, product["id"], quantity, product["unit"], product["price_cents"]),
        )
        self.store.db.commit()
        return oid

    def test_request_job_run_order_ids_are_all_recorded(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=3), "a2")
        order_id = self._create_order(message, "APL", 2)
        app = self.make_app()
        app.attach(session=object(), client=self.client, parse_service=self._stub_parse({message["id"]: {"order_id": order_id}}))
        found = app.refresh_conversations()
        app.choose_live_conversation(found[0])
        outcome = app.pipeline.process(message["id"])
        trace = outcome.trace
        self.assertTrue(trace.request_id)
        self.assertTrue(trace.job_id, "必须记录云端任务号")
        self.assertTrue(trace.run_id, "必须记录运行号")
        self.assertEqual(trace.order_id, order_id)
        self.assertIn("云端任务", trace.trace_text())


def hashlib_hex() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


if __name__ == "__main__":
    unittest.main()
