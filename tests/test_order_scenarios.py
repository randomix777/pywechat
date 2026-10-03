"""订单场景：老样子、追加、改量、取消、纠错，以及结果未知不重复执行。

这些场景同时走 **真实插件 + 真实订单规则**：模型由确定性的假后端提供，
但输出要过 `order_agent` 的结构校验，落地要过 `OrderStore` 自己的规则。
这验证的是"决策与落库是否一致"，**不是**模型的理解质量。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from business_agent import AgentCore, FakeBackend, RunStatus  # noqa: E402
from business_agent.contracts.errors import OutcomeUnknown  # noqa: E402
from business_agent.contracts.request import (  # noqa: E402
    AgentRequest, AsOf, Budget, ContextBundle, ContextEntry, Event, Policy, Scope,
)
from order_agent import OrderPlugin, RecordingLimits  # noqa: E402
from order_agent.plugin import SOURCE_CATALOG, SOURCE_CUSTOMER, SOURCE_HISTORY  # noqa: E402
from pyweixin.integration import OrderStoreApplier, build_order_request  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def uid() -> str:
    return uuid.uuid4().hex[:12]


class ScenarioBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-scen-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.store.add_product(self.tenant, "EGG", "鸡蛋", "箱", 3400, ["鸡蛋"])
        self.applier = OrderStoreApplier(self.store, self.tenant)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add_message(self, content: str, moment: datetime, source_id: str, sender: str = "张姐", day: int = 0):
        self.store.add_import(
            self.tenant, self.group,
            [{"sender": sender, "content": content, "sent_at": moment.isoformat(),
              "source_id": source_id, "stable_source_id": True}],
            source=f"import-{source_id}-{day}", source_type="test",
        )
        return dict(self.store.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? AND source_id=? AND source_id NOT LIKE '%-1'",
            (self.tenant, source_id),
        ).fetchone())

    def context(self, message, history: tuple = ()) -> ContextBundle:
        entries = [
            ContextEntry(key="customer", value={"id": self.customer, "name": "张姐"},
                         source=SOURCE_CUSTOMER, confirmed=True),
            ContextEntry(key="p:APL", value={"sku": "APL", "name": "苹果", "unit": "箱"},
                         source=SOURCE_CATALOG, confirmed=True),
            ContextEntry(key="p:EGG", value={"sku": "EGG", "name": "鸡蛋", "unit": "箱"},
                         source=SOURCE_CATALOG, confirmed=True),
        ]
        entries.extend(history)
        return ContextBundle(tuple(entries))

    def run_plugin(self, message, payload: dict, history: tuple = (), limits: RecordingLimits | None = None,
                   **request_kwargs):
        """模型 → 插件 → 结果 → 落地。一次跑完。"""
        request = build_order_request(message, tenant=self.tenant, group=self.group, **request_kwargs)
        request = request.with_context(self.context(message, history))
        core = AgentCore(backend=FakeBackend(responses=[payload]), plugin=OrderPlugin(limits=limits))
        return core.run(request)

    def apply(self, result, message):
        from business_agent.contracts.result import AgentResult

        output = dict(result.output)
        output["_message_id"] = message["id"]
        wrapped = AgentResult(
            run_id=result.run_id, request_id=result.request_id, status=result.status, output=output,
            evidence=result.evidence, ambiguities=result.ambiguities,
        )
        return self.applier.apply(wrapped, self.group)


class OrderScenarioTests(ScenarioBase):
    def test_new_order_is_recorded(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=2), "s1")
        result = self.run_plugin(message, {
            "intent": "new_order", "customer_alias": "张姐", "target_order": None,
            "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            "delivery_date": "2026-09-26", "evidence": ["苹果两箱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        })
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual([a.kind for a in result.suggested_actions], ["order.new_order"])
        applied = self.apply(result, message)
        self.assertIn(applied["outcome"], ("drafts", "review"))
        detail = self.store.order_detail(self.tenant, applied["order_id"])
        self.assertEqual(detail["items"][0]["unit_price_cents"], 2600, "金额必须由本地单价算")

    def test_lao_yang_zi_reuses_the_customers_own_history(self):
        """老样子：引用同一客户的有效历史订单。"""
        first = self.add_message("苹果两箱 鸡蛋一箱", NOW - timedelta(days=1), "old", day=1)
        order_id = "o_" + uid()
        self.store.db.execute(
            "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
            "created_at,updated_at,version,change_type,business_at_epoch) VALUES(?,?,?,?,?,?,?,?,?,1,'new',?)",
            (order_id, self.tenant, self.customer, self.group, "confirmed", "2026-09-24", first["import_id"],
             (NOW - timedelta(days=1)).isoformat(), (NOW - timedelta(days=1)).isoformat(), first["sent_at_epoch"]),
        )
        for sku, quantity, price in (("APL", 2, 2600), ("EGG", 1, 3400)):
            product = self.store.db.execute("SELECT id,unit FROM products WHERE merchant_id=? AND sku=?", (self.tenant, sku)).fetchone()
            self.store.db.execute(
                "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) VALUES(?,?,?,?,?,?)",
                ("i_" + uid(), order_id, product["id"], quantity, product["unit"], price),
            )
        self.store.db.execute(
            "INSERT INTO evidence(id,merchant_id,order_id,message_id,role) VALUES(?,?,?,?,'source')",
            ("e_" + uid(), self.tenant, order_id, first["id"]),
        )
        self.store.db.commit()

        history = (ContextEntry(
            key=f"order:{order_id}",
            value={"order_id": order_id, "status": "confirmed", "delivery_date": "2026-09-24",
                   "items": [{"sku": "APL", "quantity": 2, "unit": "箱"}, {"sku": "EGG", "quantity": 1, "unit": "箱"}]},
            source=SOURCE_HISTORY, confirmed=True, occurred_at=NOW - timedelta(days=1),
        ),)
        message = self.add_message("老样子，明天送", NOW - timedelta(hours=1), "s2")
        result = self.run_plugin(message, {
            "intent": "new_order", "customer_alias": "张姐", "target_order": order_id,
            "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["老样子"]},
                      {"sku": "EGG", "quantity": 1, "unit": "箱", "evidence": ["老样子"]}],
            "delivery_date": "2026-09-26", "evidence": ["老样子"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        }, history=history)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        applied = self.apply(result, message)
        detail = self.store.order_detail(self.tenant, applied["order_id"])
        self.assertEqual(detail["history_order_id"], order_id, "新单必须引用该客户的历史订单")
        self.assertEqual(len(detail["items"]), 2)

    def test_lao_yang_zi_without_history_is_blocked(self):
        message = self.add_message("老样子", NOW - timedelta(hours=1), "s3")
        result = self.run_plugin(message, {
            "intent": "new_order", "customer_alias": "张姐", "target_order": None,
            "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["老样子"]}],
            "delivery_date": None, "evidence": ["老样子"],
            "missing_fields": [], "ambiguities": ["no_eligible_historical_order"], "suggested_change": None,
        })
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW, "没有可对应历史时不得猜")
        self.assertEqual(result.suggested_actions, ())

    def test_modify_replaces_the_previous_version(self):
        base = self.add_message("苹果两箱", NOW - timedelta(hours=3), "s4")
        order_id = self._draft(base, "APL", 2)
        message = self.add_message("改成三箱", NOW - timedelta(hours=1), "s5")
        result = self.run_plugin(message, {
            "intent": "modify", "customer_alias": "张姐", "target_order": order_id,
            "items": [{"sku": "APL", "quantity": 3, "unit": "箱", "evidence": ["三箱"]}],
            "delivery_date": None, "evidence": ["三箱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        })
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        applied = self.apply(result, message)
        new_id = applied["order_id"]
        self.assertNotEqual(new_id, order_id, "改量必须产生新版本，不得就地改写")
        old = self.store.order_detail(self.tenant, order_id)
        self.assertEqual(old["items"][0]["quantity"], 2, "旧版本内容必须保持不变")
        new = self.store.order_detail(self.tenant, new_id)
        self.assertEqual(new["items"][0]["quantity"], 3)
        self.assertEqual(new["history_order_id"], order_id)

    def test_modify_without_a_resolved_target_is_blocked(self):
        base = self.add_message("苹果两箱", NOW - timedelta(hours=3), "s6")
        real_order = self._draft(base, "APL", 2)
        message = self.add_message("改成三箱", NOW - timedelta(hours=1), "s7")
        result = self.run_plugin(message, {
            "intent": "modify", "customer_alias": "张姐", "target_order": "编出来的订单号",
            "items": [{"sku": "APL", "quantity": 3, "unit": "箱", "evidence": ["三箱"]}],
            "delivery_date": None, "evidence": ["三箱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        }, known_order_ids=(real_order,))
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn("target_order_unresolved", {a.code for a in result.ambiguities})
        self.assertEqual(result.suggested_actions, ())

    def test_cancel_produces_a_reviewable_cancellation(self):
        base = self.add_message("苹果两箱", NOW - timedelta(hours=3), "s8")
        order_id = self._draft(base, "APL", 2)
        message = self.add_message("不要了", NOW - timedelta(hours=1), "s9")
        result = self.run_plugin(message, {
            "intent": "cancel", "customer_alias": "张姐", "target_order": order_id,
            "items": [], "delivery_date": None, "evidence": ["不要了"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        })
        applied = self.apply(result, message)
        new_id = applied["order_id"]
        detail = self.store.order_detail(self.tenant, new_id)
        self.assertIn(detail["status"], ("cancel_review", "review"))

    def test_inquiry_creates_no_order(self):
        message = self.add_message("苹果多少钱", NOW - timedelta(hours=1), "s10")
        result = self.run_plugin(message, {
            "intent": "inquiry", "customer_alias": "张姐", "target_order": None,
            "items": [], "delivery_date": None, "evidence": ["苹果多少钱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        })
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.suggested_actions, (), "询价不建单")
        before = self.store.db.execute("SELECT COUNT(*) AS c FROM orders WHERE merchant_id=?", (self.tenant,)).fetchone()["c"]
        self.apply(result, message)
        after = self.store.db.execute("SELECT COUNT(*) AS c FROM orders WHERE merchant_id=?", (self.tenant,)).fetchone()["c"]
        self.assertEqual(before, after, "询价不得产生任何订单记录")

    def test_payment_claim_never_creates_a_paid_order(self):
        message = self.add_message("钱转了", NOW - timedelta(hours=1), "s11")
        result = self.run_plugin(message, {
            "intent": "payment_claim", "customer_alias": "张姐", "target_order": None,
            "items": [], "delivery_date": None, "evidence": ["钱转了"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        })
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        self.assertIn("payment_claim_not_receipt", {a.code for a in result.ambiguities})
        self.assertEqual(result.suggested_actions, ())

    def test_ambiguity_asks_at_most_one_necessary_question(self):
        message = self.add_message("来两箱那个", NOW - timedelta(hours=1), "s12")
        result = self.run_plugin(message, {
            "intent": "new_order", "customer_alias": "张姐", "target_order": None,
            "items": [{"sku": None, "quantity": 2, "unit": "箱", "evidence": ["来两箱那个"]}],
            "delivery_date": None, "evidence": ["来两箱那个"],
            "missing_fields": ["sku"], "ambiguities": ["sku_not_in_catalog"], "suggested_change": None,
        })
        self.assertEqual(result.status, RunStatus.NEEDS_REVIEW)
        plugin = OrderPlugin()
        question = plugin.follow_up_question(result.output, build_order_request(message, tenant=self.tenant, group=self.group))
        self.assertIsNotNone(question)
        self.assertEqual(len(question.fields), 1, "最多问一个问题")

    def test_correction_supersedes_the_old_order(self):
        """纠错后旧订单被替代，不得两单并存。"""
        base = self.add_message("苹果两箱", NOW - timedelta(hours=3), "s13")
        order_id = self._draft(base, "APL", 2)
        message = self.add_message("改成四箱", NOW - timedelta(hours=1), "s14")
        result = self.run_plugin(message, {
            "intent": "modify", "customer_alias": "张姐", "target_order": order_id,
            "items": [{"sku": "APL", "quantity": 4, "unit": "箱", "evidence": ["四箱"]}],
            "delivery_date": None, "evidence": ["四箱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        })
        new_id = self.apply(result, message)["order_id"]
        self.store.confirm(self.tenant, new_id, "u1")
        old = self.store.order_detail(self.tenant, order_id)
        self.assertEqual(old["status"], "superseded", "确认新版本后旧版本必须被替代")
        live = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE merchant_id=? AND status IN ('draft','review','confirmed')",
            (self.tenant,),
        ).fetchone()
        self.assertEqual(int(live["c"]), 1, "同一笔订单不得两单并存")

    def test_applying_the_same_result_twice_does_not_double_order(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=1), "s15")
        result = self.run_plugin(message, {
            "intent": "new_order", "customer_alias": "张姐", "target_order": None,
            "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            "delivery_date": None, "evidence": ["苹果两箱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        })
        first = self.apply(result, message)
        second = self.apply(result, message)
        self.assertEqual(second["outcome"], "duplicate")
        orders = self.store.list_orders(self.tenant)
        self.assertEqual(len(orders), 1)

    def _draft(self, message, sku, quantity) -> str:
        oid = "o_" + uid()
        self.store.db.execute(
            "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
            "created_at,updated_at,version,change_type,business_at_epoch) VALUES(?,?,?,?,'draft',?,?,?,?,1,'new',?)",
            (oid, self.tenant, self.customer, self.group, "2026-09-26", message["import_id"],
             NOW.isoformat(), NOW.isoformat(), message["sent_at_epoch"]),
        )
        product = self.store.db.execute(
            "SELECT id,price_cents,unit FROM products WHERE merchant_id=? AND sku=?", (self.tenant, sku)
        ).fetchone()
        self.store.db.execute(
            "INSERT INTO order_items(id,order_id,product_id,quantity,unit,unit_price_cents) VALUES(?,?,?,?,?,?)",
            ("i_" + uid(), oid, product["id"], quantity, product["unit"], product["price_cents"]),
        )
        self.store.db.execute("INSERT INTO evidence(id,merchant_id,order_id,message_id,role) VALUES(?,?,?,?,'source')",
                              ("e_" + uid(), self.tenant, oid, message["id"]))
        self.store.db.commit()
        return oid


class OutcomeUnknownTests(ScenarioBase):
    def test_unknown_outcome_is_not_re_executed(self):
        """请求可能已提交时不得重跑，否则同一笔工作可能被计费两次。"""
        message = self.add_message("苹果两箱", NOW - timedelta(hours=1), "u1")
        backend = FakeBackend(failures=[OutcomeUnknown("连接中断，结果未知")])
        core = AgentCore(backend=backend, plugin=OrderPlugin())
        request = build_order_request(message, tenant=self.tenant, group=self.group).with_context(self.context(message))
        result = core.run(request)
        self.assertEqual(result.status, RunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(backend.call_count, 1, "结果未知时不得自动重跑")
        self.assertFalse(result.settled)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM orders WHERE merchant_id=?", (self.tenant,)).fetchone()["c"], 0)

    def test_network_error_before_submission_is_retried(self):
        from business_agent.contracts.errors import NetworkError

        message = self.add_message("苹果两箱", NOW - timedelta(hours=1), "u2")
        backend = FakeBackend(failures=[NetworkError("连不上"), None], responses=[{
            "intent": "new_order", "customer_alias": "张姐", "target_order": None,
            "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            "delivery_date": None, "evidence": ["苹果两箱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        }])
        core = AgentCore(backend=backend, plugin=OrderPlugin())
        request = build_order_request(message, tenant=self.tenant, group=self.group).with_context(self.context(message))
        result = core.run(request)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(backend.call_count, 2, "确定未执行的失败可以有限重试")

    def test_retrying_the_same_request_id_is_free(self):
        message = self.add_message("苹果两箱", NOW - timedelta(hours=1), "u3")
        payload = {
            "intent": "new_order", "customer_alias": "张姐", "target_order": None,
            "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            "delivery_date": None, "evidence": ["苹果两箱"],
            "missing_fields": [], "ambiguities": [], "suggested_change": None,
        }
        backend = FakeBackend(responses=[payload])
        core = AgentCore(backend=backend, plugin=OrderPlugin())
        request = build_order_request(message, tenant=self.tenant, group=self.group).with_context(self.context(message))
        first = core.run(request)
        second = core.run(request)
        self.assertEqual(backend.call_count, 1, "同一请求重复提交不得再次执行")
        self.assertEqual(first.run_id, second.run_id)


if __name__ == "__main__":
    unittest.main()
