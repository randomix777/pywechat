"""回执与自动接单的端到端行为。

覆盖商户最容易吃亏的几件事：
* 订单没保存却发出「已记单」；
* 崩溃后反复发同一条回执；
* 暂停后仍然发出去；
* 订单更新了还发旧版本回执；
* 自己的回执又触发新订单；
* 历史导入给客户发一堆「已记单」。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
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

from pyweixin.application import MerchantApplication  # noqa: E402
from pyweixin.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from pyweixin.history_read import SimulatedHistory  # noqa: E402
from pyweixin.local_parse import ParseProgress  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.receipts import (  # noqa: E402
    NO_RECEIPT_SOURCES,
    RECEIPT_CONFIRMATION,
    ReceiptOutbox,
    ReceiptRequest,
    ReceiptStatus,
    build_receipt_text,
)
from pyweixin.runmode import AutoOrderSettings, RunMode  # noqa: E402
from pyweixin.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
SAMPLE_RECEIPT = "好的，已记单：苹果 2箱，合计 52.00（直接回复即可修改）"


def uid() -> str:
    return uuid.uuid4().hex[:12]


class ReceiptBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-rcpt-"))
        from pyweixin.agent import AgentStore

        self.agent_store = AgentStore(str(self.dir / "agent.db"))
        self.sent: list[tuple[str, str]] = []
        self.outbox = ReceiptOutbox(self.agent_store, sender=lambda t, x: self.sent.append((t, x)))

    def tearDown(self):
        self.outbox.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, **kwargs) -> ReceiptRequest:
        defaults = dict(
            order_id=kwargs.pop("order_id", "o_" + uid()),
            order_version=1,
            conversation="测试群",
            kind=RECEIPT_CONFIRMATION,
            text=SAMPLE_RECEIPT,
        )
        defaults.update(kwargs)
        return ReceiptRequest(**defaults)


class OutboxTests(ReceiptBase):
    def test_same_order_version_queues_only_once(self):
        request = self.request()
        self.assertTrue(self.outbox.enqueue(request))
        self.assertFalse(self.outbox.enqueue(request), "同一幂等键不得产生第二条回执")
        self.assertEqual(self.outbox.pending_count(), 1)

    def test_new_version_gets_its_own_receipt(self):
        self.assertTrue(self.outbox.enqueue(self.request(order_version=1)))
        self.assertTrue(self.outbox.enqueue(self.request(order_version=2)))
        self.assertEqual(self.outbox.pending_count(), 2)

    def test_dispatch_sends_and_marks_sent(self):
        self.outbox.enqueue(self.request())
        outcomes = self.outbox.dispatch(allowed_targets=("测试群",))
        self.assertEqual(outcomes[0]["status"], ReceiptStatus.SENT)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.outbox.pending_count(), 0)

    def test_paused_blocks_sending_but_keeps_the_receipt(self):
        self.outbox.enqueue(self.request())
        outcomes = self.outbox.dispatch(allowed_targets=("测试群",), paused=True)
        self.assertEqual(outcomes[0]["status"], ReceiptStatus.HELD)
        self.assertEqual(self.sent, [], "暂停时不得发送")
        self.agent_store.release_held()
        self.outbox.dispatch(allowed_targets=("测试群",), paused=False)
        self.assertEqual(len(self.sent), 1)

    def test_unallowed_conversation_is_never_sent(self):
        self.outbox.enqueue(self.request(conversation="别的群"))
        outcomes = self.outbox.dispatch(allowed_targets=("测试群",))
        self.assertEqual(outcomes[0]["reason"], "conversation_not_allowed")
        self.assertEqual(self.sent, [])

    def test_stale_order_version_is_not_sent(self):
        self.outbox.enqueue(self.request(order_version=1))
        outcomes = self.outbox.dispatch(allowed_targets=("测试群",), expected_versions={"测试群": 3})
        self.assertEqual(outcomes[0]["reason"], "stale_order_version")
        self.assertEqual(self.sent, [], "旧版本回执会让客户看到过期内容")

    def test_crash_during_send_does_not_resend(self):
        calls: list[str] = []

        def flaky(target, text):
            calls.append(text)
            raise RuntimeError("发送中断，结果不明")

        outbox = ReceiptOutbox(self.agent_store, sender=flaky)
        outbox.enqueue(self.request())
        outcomes = outbox.dispatch(allowed_targets=("测试群",))
        self.assertEqual(outcomes[0]["status"], ReceiptStatus.SENT_UNKNOWN)
        self.assertEqual(len(calls), 1)
        outbox.dispatch(allowed_targets=("测试群",))
        self.assertEqual(len(calls), 1, "结果未知的回执不得自动重发")
        self.assertEqual(outbox.unknown_count(), 1)

    def test_reconcile_confirms_already_sent(self):
        outbox = ReceiptOutbox(self.agent_store, sender=self._always_fails)
        outbox.enqueue(self.request())
        outbox.dispatch(allowed_targets=("测试群",))
        resolved = outbox.reconcile_unknown(verifier=lambda target, text: True)
        self.assertEqual(resolved[0]["status"], ReceiptStatus.SENT)
        self.assertEqual(outbox.unknown_count(), 0)

    def test_reconcile_requeues_when_confirmed_not_sent(self):
        outbox = ReceiptOutbox(self.agent_store, sender=self._always_fails)
        outbox.enqueue(self.request())
        outbox.dispatch(allowed_targets=("测试群",))
        resolved = outbox.reconcile_unknown(verifier=lambda target, text: False)
        self.assertEqual(resolved[0]["status"], ReceiptStatus.QUEUED)
        self.assertEqual(outbox.unknown_count(), 0)

    def test_reconcile_without_verifier_keeps_unknown(self):
        outbox = ReceiptOutbox(self.agent_store, sender=self._always_fails)
        outbox.enqueue(self.request())
        outbox.dispatch(allowed_targets=("测试群",))
        resolved = outbox.reconcile_unknown()
        self.assertEqual(resolved[0]["status"], ReceiptStatus.SENT_UNKNOWN)
        self.assertEqual(outbox.unknown_count(), 1, "无法确认时保持未确认，不重发")

    @staticmethod
    def _always_fails(target, text):
        raise RuntimeError("断了")

    def test_receipt_lease_recovers_after_crash(self):
        from pyweixin.agent import AgentStore

        self.outbox.enqueue(self.request())
        leases = self.agent_store.claim_outbox_leases(limit=5, lease_seconds=0.01)
        self.assertEqual(len(leases), 1)
        time.sleep(0.05)
        self.agent_store.close()
        reopened = AgentStore(str(self.dir / "agent.db"))
        try:
            recovered = reopened.recover_outbox()
            self.assertEqual(recovered, 1, "租约过期后重启应回收 processing")
            self.assertEqual(reopened.pending_count(), 1)
        finally:
            reopened.close()

    def test_unexpired_lease_is_not_stolen(self):
        self.outbox.enqueue(self.request())
        self.agent_store.claim_outbox_leases(limit=5, lease_seconds=300)
        self.assertEqual(self.agent_store.recover_outbox(), 0, "租约未过期不得回收")
        # 仍在别人手里：再领一次拿不到
        self.assertEqual(len(self.agent_store.claim_outbox_leases(limit=5, lease_seconds=300)), 0)

    def test_history_sources_never_queue_a_receipt(self):
        for source in sorted(NO_RECEIPT_SOURCES):
            self.assertFalse(self.outbox.enqueue(self.request(source=source)), source)

    def test_empty_text_is_refused(self):
        self.assertFalse(self.outbox.enqueue(self.request(text="   ")))

    def test_own_receipt_is_recognised(self):
        self.assertFalse(self.outbox.is_own_receipt("苹果两箱"))
        self.outbox.mark_sent(SAMPLE_RECEIPT)
        self.assertTrue(self.outbox.is_own_receipt(SAMPLE_RECEIPT))

    def test_own_receipt_is_recognised_after_a_cold_start(self):
        """没发过任何回执就重启时，靠形状也能认出自己发的内容。"""
        fresh = ReceiptOutbox(self.agent_store, sender=lambda t, x: None)
        self.assertTrue(fresh.is_own_receipt(SAMPLE_RECEIPT))
        self.assertFalse(fresh.is_own_receipt("苹果两箱明天送"))


class ReceiptTextTests(unittest.TestCase):
    def test_missing_price_is_refused_rather_than_showing_zero(self):
        with self.assertRaises(ValueError):
            build_receipt_text([{"name": "苹果", "quantity": 2, "unit": "箱"}])

    def test_price_pending_says_so_instead_of_zero(self):
        text = build_receipt_text([{"name": "苹果", "quantity": 2, "unit": "箱"}], price_pending=True)
        self.assertIn("价格稍后确认", text)
        self.assertNotIn("0.00", text)

    def test_receipt_contains_no_address_or_phone(self):
        text = build_receipt_text(
            [{"name": "苹果", "quantity": 2, "unit": "箱", "unit_price_cents": 2600}],
            delivery_date="2026-09-26",
        )
        for leak in ("@", "电话", "手机", "地址", "路", "号"):
            self.assertNotIn(leak, text, f"回执不应包含 {leak}")

    def test_cancel_receipt_wording(self):
        text = build_receipt_text(
            [{"name": "苹果", "quantity": 2, "unit": "箱", "unit_price_cents": 2600}], kind="cancel"
        )
        self.assertIn("取消", text)


class PipelineReceiptTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-pipe-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.sent: list[tuple[str, str]] = []
        self.app: MerchantApplication | None = None

    def tearDown(self):
        if self.app is not None:
            self.app.close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add_message(self, content: str, moment: datetime, source_id: str, sender: str = "张姐"):
        self.store.add_import(
            self.tenant, self.group,
            [{"sender": sender, "content": content, "sent_at": moment.isoformat(),
              "source_id": source_id, "stable_source_id": True}],
            source=f"import-{source_id}", source_type="test",
        )
        return dict(self.store.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? AND source_id=?", (self.tenant, source_id)
        ).fetchone())

    def make_order(self, message, sku="APL", quantity=2, status="draft", change_type="new", version=1):
        oid = "o_" + uid()
        self.store.db.execute(
            "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
            "created_at,updated_at,version,change_type,business_at_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, self.tenant, self.customer, self.group, status, "2026-09-26", message["import_id"],
             NOW.isoformat(), NOW.isoformat(), version, change_type, message["sent_at_epoch"]),
        )
        product = self.store.db.execute(
            "SELECT id,price_cents,unit FROM products WHERE merchant_id=? AND sku=?", (self.tenant, sku)
        ).fetchone()
        self.store.db.execute(
            "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) VALUES(?,?,?,?,?,?)",
            ("i_" + uid(), oid, product["id"], quantity, product["unit"], product["price_cents"]),
        )
        self.store.db.commit()
        return oid

    def stub_parse(self, mapping: dict[str, str], model_executed: bool = True):
        service = type("Stub", (), {})()
        service.submitted: list[str] = []
        service.model_executed = model_executed

        def submit(message_id, on_progress=None):
            service.submitted.append(message_id)
            return ParseProgress("done", "已生成待确认订单", message_id=message_id, job_id=f"job_{uid()[:6]}")

        def settle(on_progress=None):
            if not service.submitted:
                return ParseProgress("pending", "没有待处理的解析")
            message_id = service.submitted[-1]
            return ParseProgress("done", "已生成待确认订单", message_id=message_id, order_id=mapping.get(message_id, ""))

        service.submit = submit
        service.settle = settle
        return service

    def build(self, auto: AutoOrderSettings, mapping: dict[str, str], model_executed: bool = True,
              allow_sending: bool = True):
        self.app = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant,
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=SimulatedConversations([(self.group, ConversationKind.GROUP, 128)]),
            history_reader_fn=SimulatedHistory(default_count=0),
            simulated=True,
        )
        client = type("C", (), {
            "account": lambda self: {"subscription": {"active": True, "remaining_micro_usd": 99}}
        })()
        self.app.attach(object(), client, self.stub_parse(mapping, model_executed), backend_is_model=model_executed)
        self.app.auto = auto
        self.app.settings.auto = auto
        if allow_sending:
            # 真实发送在真机验证前是关着的。要测发送路径的测试必须显式
            # 打开它，并提供一份"目标干净"的检查结果。
            from pyweixin.wechat_send import StaticInspector, TargetState, mark_real_sending_verified

            mark_real_sending_verified(True)
            self.addCleanup(mark_real_sending_verified, False)
            self.app.enable_target_inspection(
                StaticInspector(
                    TargetState(
                        conversation=self.group, draft_text="", account_id="",
                        window_ready=True, observed=True,
                    )
                ),
                raw_sender=lambda t, x: self.sent.append((t, x)),
            )
        self.app.choose_live_conversation(self.app.refresh_conversations()[0])
        return self.app

    def test_receipt_is_queued_only_after_the_order_exists(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p1")
        order_id = self.make_order(message)
        app = self.build(AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=True), {message["id"]: order_id})
        outcome = app.pipeline.process(message["id"])
        self.assertTrue(outcome.receipt_queued)
        self.assertTrue(self.store.order_detail(self.tenant, order_id))
        self.assertEqual(app.outbox.pending_count(), 1)

    def test_history_source_queues_no_receipt(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p2")
        order_id = self.make_order(message)
        app = self.build(AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=True), {message["id"]: order_id})
        outcome = app.pipeline.process(message["id"], source="history")
        self.assertFalse(outcome.receipt_queued, "历史整理不得给客户发回执")
        self.assertEqual(app.outbox.pending_count(), 0)

    def test_background_mode_records_but_never_sends(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p3")
        order_id = self.make_order(message)
        app = self.build(AutoOrderSettings(mode=RunMode.BACKGROUND, allow_local_automation=True), {message["id"]: order_id})
        outcome = app.pipeline.process(message["id"])
        self.assertEqual(outcome.order_id, order_id, "后台整理仍然记单")
        self.assertFalse(outcome.receipt_queued, "后台整理不得发送")
        app.dispatch_receipts()
        self.assertEqual(self.sent, [])

    def test_not_allowing_local_automation_blocks_sending(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p4")
        order_id = self.make_order(message)
        app = self.build(AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=False), {message["id"]: order_id})
        outcome = app.pipeline.process(message["id"])
        self.assertFalse(outcome.receipt_queued)
        self.assertIn("尚未允许本机自动操作", app.auto.describe())

    def test_pause_blocks_dispatch_immediately(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p5")
        order_id = self.make_order(message)
        app = self.build(AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=True), {message["id"]: order_id})
        app.pipeline.process(message["id"])
        app.pause()
        app.dispatch_receipts()
        self.assertEqual(self.sent, [], "暂停后立即阻止发送")
        app.set_mode(RunMode.AUTO_TAKE)
        app.dispatch_receipts()
        self.assertEqual(len(self.sent), 1, "恢复后继续发送")

    def test_own_receipt_does_not_create_a_new_order(self):
        app = self.build(AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=True), {})
        app.outbox.mark_sent(SAMPLE_RECEIPT)
        message = self.add_message(SAMPLE_RECEIPT, NOW - timedelta(hours=1), "p6")
        before = self.store.db.execute("SELECT COUNT(*) AS c FROM orders WHERE merchant_id=?", (self.tenant,)).fetchone()["c"]
        outcome = app.pipeline.process(message["id"])
        after = self.store.db.execute("SELECT COUNT(*) AS c FROM orders WHERE merchant_id=?", (self.tenant,)).fetchone()["c"]
        self.assertEqual(outcome.outcome, "own_receipt")
        self.assertEqual(before, after, "自己的回执不得再次触发建单")

    def test_demonstration_result_is_flagged(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p7")
        order_id = self.make_order(message)
        app = self.build(
            AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=True),
            {message["id"]: order_id}, model_executed=False,
        )
        outcome = app.pipeline.process(message["id"])
        self.assertTrue(outcome.simulated, "假后端结果必须标记为演示")
        self.assertIn("演示结果", outcome.message)
        self.assertFalse(outcome.trace.model_executed)

    def test_missing_price_does_not_queue_a_zero_receipt(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p8")
        order_id = self.make_order(message)
        self.store.db.execute("UPDATE order_items SET unit_price_cents=0 WHERE order_id=?", (order_id,))
        self.store.db.commit()
        auto = AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=True, allow_price_pending=False)
        app = self.build(auto, {message["id"]: order_id})
        outcome = app.pipeline.process(message["id"])
        self.assertFalse(outcome.receipt_queued, "缺价不得发出零元回执")

    def test_price_pending_queues_a_pending_receipt(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "p9")
        order_id = self.make_order(message)
        self.store.db.execute("UPDATE order_items SET unit_price_cents=0 WHERE order_id=?", (order_id,))
        self.store.db.commit()
        auto = AutoOrderSettings(mode=RunMode.AUTO_TAKE, allow_local_automation=True, allow_price_pending=True)
        app = self.build(auto, {message["id"]: order_id})
        outcome = app.pipeline.process(message["id"])
        self.assertTrue(outcome.receipt_queued)
        app.dispatch_receipts()
        self.assertIn("价格稍后确认", self.sent[0][1])
        self.assertNotIn("0.00", self.sent[0][1])


if __name__ == "__main__":
    unittest.main()