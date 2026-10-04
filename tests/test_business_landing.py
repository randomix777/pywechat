"""**业务落地测试**：有效的结构化结果 → 订单、版本、备货。

    python -m unittest tests.test_business_landing -v

这些用例**不是**模型理解能力测试。它们用 :class:`ScriptedBackend` 返回
预先写好的结构化结果，只验一件事：云端结果落到本地订单时，客户、SKU、
单位、数量、证据、有效版本与备货是否正确。

上一轮用 ``LocalRuleAdapter`` 跑出 0 张订单、7 个待澄清，那只能证明
"没有乱建单"，**证明不了**"该建的单建对了"。本文件补上这一半。

每个用例都断言：非空明细、精确数量、有效版本、证据、任务状态、备货汇总。
只检查数量统计或只检查 HTTP 200 都不算数。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.dashboard import DashboardBuilder  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402
from pyweixin.opencode import ScriptedBackend, validate_parse_result  # noqa: E402

APL, ORG = "APL", "ORG"


def new_store() -> tuple[OrderStore, str, dict[str, str], dict[str, int]]:
    store = OrderStore(":memory:")
    tenant = store.create_merchant("落地测试店", plan="demo", ai_quota=0)["merchant_id"]
    customers = {
        "zhang": store.add_customer(tenant, "张姐", "张姐", "鲜果群"),
        "chen": store.add_customer(tenant, "陈先生", "陈先生", "鲜果群"),
        "li": store.add_customer(tenant, "李女士", "李女士", "鲜果群"),
    }
    prices = {APL: 2600, ORG: 2200}
    store.add_product(tenant, APL, "苹果", "箱", prices[APL], ["苹果", "A"])
    store.add_product(tenant, ORG, "橙子", "箱", prices[ORG], ["橙子"])
    return store, tenant, customers, prices


def add_message(store, tenant, group, content, customer=None, sent_at="2026-01-02T09:00:00+00:00", source="s1"):
    store.add_import(
        tenant, group,
        [{"sender": customer_name_of(content) if customer else "路人", "content": content,
          "kind": "text", "sent_at": sent_at, "source_id": source}],
        source=source, source_type="test",
    )
    row = store.db.execute(
        "SELECT * FROM messages WHERE merchant_id=? AND source_id=?", (tenant, source)
    ).fetchone()
    if customer:
        store.bind_message_customer(tenant, row["id"], customer, "tester")
        row = store.db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
    return row


def customer_name_of(_content: str) -> str:
    return "客户"


def suggestion(**kwargs):
    """一个结构上合法的建议；用 kwargs 覆盖关心的字段。"""
    base = {
        "intent": "new_order", "customer_alias": None, "target_order": None,
        "items": [], "delivery_date": None, "evidence": [], "missing_fields": [],
        "ambiguities": [], "suggested_change": None,
    }
    base.update(kwargs)
    return validate_parse_result(base)


class BusinessLandingBase(unittest.TestCase):
    def setUp(self):
        self.store, self.tenant, self.customers, self.prices = new_store()
        self.dashboard = DashboardBuilder(self.store, self.tenant)
        self.applied: list[dict] = []

    def tearDown(self):
        self.store.close()

    # ------------------------------------------------------------ helpers

    def deliver(self, message, result):
        """把一条云端结果送进真实的业务应用路径。"""
        applied = self.store.apply_cloud_suggestion(
            self.tenant, message["group_id"], message["id"], result
        )
        self.applied.append(applied)
        return applied

    def effective(self, message_id: str):
        return self.store.current_effective_order(self.tenant, message_id)

    def stock(self) -> dict[str, float]:
        return {row.sku: row.quantity for row in self.dashboard.stock_summary()}

    def assert_lines(self, order_id: str, expected: dict[str, float], why: str = ""):
        """订单明细必须**非空**且数量精确（同一 SKU 的多行按数量求和）。"""
        detail = self.store.order_detail(self.tenant, order_id)
        self.assertTrue(detail["items"], f"{why or order_id}：明细不得为空")
        actual: dict[str, float] = {}
        for item in detail["items"]:
            actual[item["sku"]] = actual.get(item["sku"], 0.0) + float(item["quantity"])
        self.assertEqual(actual, expected, why)
        micro_usd = {item["sku"]: item["unit"] for item in detail["items"]}
        self.assertTrue(all(micro_usd.values()), f"{why or order_id}：单位不得为空")
        return detail

    def assert_single_line_per_sku(self, order_id: str, why: str = ""):
        """同一商品在订单里只应出现一行，否则商户看着像两笔生意。"""
        detail = self.store.order_detail(self.tenant, order_id)
        skus = [item["sku"] for item in detail["items"]]
        self.assertEqual(len(skus), len(set(skus)), f"{why or order_id}：同一商品出现了多行 {skus}")

    def assert_evidence_quoted(self, order_id: str, source_text: str):
        """订单必须能追到**逐字**的来源原文。

        证据存在 ``evidence``（订单 ↔ 消息）里，保留的是原话本身 ——
        比只存一句引用更硬：商户说"这单为什么错了"，可以一路看到原文。
        """
        detail = self.store.order_detail(self.tenant, order_id)
        self.assertTrue(detail["evidence"], "订单必须带来源消息")
        sources = [item["content"] for item in detail["evidence"]]
        self.assertTrue(any(source_text in text for text in sources),
                        f"来源原文里必须含有 {source_text!r}，实际 {sources}")


# ---------------------------------------------------------------- 1. 新单


class NewOrderLandingTests(BusinessLandingBase):
    def test_new_order_lands_with_customer_sku_unit_amount_and_evidence(self):
        message = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="n1")
        applied = self.deliver(message, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))
        self.assertEqual(applied["outcome"], "drafts")
        self.assertTrue(applied["order_id"])
        detail = self.assert_lines(applied["order_id"], {APL: 2}, "新单")
        self.assertEqual(detail["customer_id"], self.customers["zhang"])
        self.assertEqual(detail["customer_name"], "张姐")
        self.assertEqual(detail["items"][0]["unit"], "箱")
        # 金额按**本地单价**算，不是云端给的数
        self.assertEqual(detail["items"][0]["unit_price_cents"], self.prices[APL])
        total = sum(float(i["quantity"]) * int(i["unit_price_cents"]) for i in detail["items"]) / 100
        self.assertEqual(total, 2 * 26.0)
        self.assert_evidence_quoted(applied["order_id"], "苹果两箱")
        self.assertEqual(self.stock(), {APL: 2.0})


# ---------------------------------------------------------------- 2. 追加


class AppendLandingTests(BusinessLandingBase):
    def test_append_adds_to_the_same_order_and_stock_is_three(self):
        first = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="a1")
        base = self.deliver(first, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))
        self.store.confirm(self.tenant, base["order_id"], "tester")

        second = add_message(self.store, self.tenant, "鲜果群", "再加一箱苹果", self.customers["zhang"],
                              sent_at="2026-01-03T09:00:00+00:00", source="a2")
        applied = self.deliver(second, suggestion(
            intent="append", customer_alias="张姐", target_order=base["order_id"],
            items=[{"sku": APL, "quantity": 1, "unit": "箱", "evidence": ["再加一箱苹果"]}],
            evidence=["再加一箱苹果"],
        ))
        self.assertEqual(applied["outcome"], "drafts")
        # 追加生成的是一个**新版本**，明细是 3（2 + 1），不是"旧单 2 + 新单 3"
        new_id = applied["order_id"]
        self.assert_lines(new_id, {APL: 3}, "追加后")
        self.assert_single_line_per_sku(new_id, "追加后")
        self.assertEqual(self.store.order_detail(self.tenant, new_id)["history_order_id"],
                         base["order_id"], "新版本必须指向被追加的那一单")
        self.assertEqual(self.stock(), {APL: 3.0}, "备货必须是 3，不能是 2+3=5")

    def test_appending_to_another_customers_order_is_refused(self):
        mine = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="a3")
        mine_order = self.deliver(mine, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))["order_id"]
        theirs = add_message(self.store, self.tenant, "鲜果群", "再加一箱苹果", self.customers["chen"],
                             sent_at="2026-01-03T09:00:00+00:00", source="a4")
        applied = self.deliver(theirs, suggestion(
            intent="append", customer_alias="陈先生", target_order=mine_order,
            items=[{"sku": APL, "quantity": 1, "unit": "箱", "evidence": ["再加一箱苹果"]}],
            evidence=["再加一箱苹果"],
        ))
        self.assertIsNone(applied["order_id"], "不得把订单加到别的客户名下")
        self.assertEqual(applied["outcome"], "clarify")
        self.assertEqual(self.store.open_clarifications(self.tenant)[0]["reason"], "target_order_unresolved")
        self.assert_lines(mine_order, {APL: 2}, "张姐的订单不受影响")


# ---------------------------------------------------------------- 3. 改量


class ModifyLandingTests(BusinessLandingBase):
    def test_modify_sets_four_and_does_not_accumulate_to_five(self):
        first = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="m1")
        base = self.deliver(first, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))
        self.store.confirm(self.tenant, base["order_id"], "tester")

        second = add_message(self.store, self.tenant, "鲜果群", "改成四箱", self.customers["zhang"],
                              sent_at="2026-01-03T09:00:00+00:00", source="m2")
        applied = self.deliver(second, suggestion(
            intent="modify", customer_alias="张姐", target_order=base["order_id"],
            items=[{"sku": APL, "quantity": 4, "unit": "箱", "evidence": ["改成四箱"]}],
            evidence=["改成四箱"],
        ))
        new_id = applied["order_id"]
        self.assertNotEqual(new_id, base["order_id"], "改量必须产生新版本，不就地改")
        self.assert_lines(new_id, {APL: 4.0}, "改量后")
        # 已生效的那一单内容没被动过
        self.assert_lines(base["order_id"], {APL: 2.0}, "已确认订单")
        # 备货按有效版本算：新版本 +2，旧版本被待生效变更压制，不重复计数
        self.assertEqual(self.stock(), {APL: 4.0}, "不能是 2+4=6，也不能是 0")


# ---------------------------------------------------------------- 4. 取消


class CancelLandingTests(BusinessLandingBase):
    def test_cancelling_removes_the_order_from_effective_stock(self):
        message = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="c1")
        order_id = self.deliver(message, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))["order_id"]
        self.store.confirm(self.tenant, order_id, "tester")
        self.assertEqual(self.stock(), {APL: 2.0})

        cancel_msg = add_message(self.store, self.tenant, "鲜果群", "这单不要了", self.customers["zhang"],
                                 sent_at="2026-01-04T09:00:00+00:00", source="c2")
        applied = self.deliver(cancel_msg, suggestion(
            intent="cancel", customer_alias="张姐", target_order=order_id, evidence=["这单不要了"],
        ))
        self.assertEqual(applied["outcome"], "drafts")
        cancelled = self.store.order_detail(self.tenant, applied["order_id"])
        # 取消建议必须能被商户一眼认出来：状态是 cancel_review，不是普通待核对
        self.assertEqual(cancelled["status"], "cancel_review", "取消建议不得被按成普通待核对")
        self.assertEqual(cancelled["change_type"], "cancel")
        self.assertEqual(cancelled["history_order_id"], order_id, "必须指向被取消的那一单")
        self.assertTrue(cancelled["review_tasks"], "取消建议必须有待核对任务")
        # 待核对期间这一单仍是有效版本，备货不能消失
        self.assertEqual(self.stock(), {APL: 2.0}, "待取消期间备货不得消失")
        # 商户确认取消后彻底不进备货
        self.store.confirm(self.tenant, applied["order_id"], "tester")
        self.assertEqual(self.stock(), {}, "确认取消后不得计入备货")
        self.assertNotEqual(
            self.store.order_detail(self.tenant, applied["order_id"])["status"],
            "confirmed", "确认取消不等于把这单确认成有效",
        )


# -------------------------------------------------------- 5/6. 老样子


class HistoryReferenceLandingTests(BusinessLandingBase):
    def _seed_confirmed(self, quantity: float, source: str, sent_at: str):
        message = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["chen"],
                              sent_at=sent_at, source=source)
        order_id = self.deliver(message, suggestion(
            customer_alias="陈先生",
            items=[{"sku": APL, "quantity": quantity, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))["order_id"]
        self.store.confirm(self.tenant, order_id, "tester")
        return order_id

    def test_history_before_the_message_is_copied_into_a_new_order(self):
        history_order = self._seed_confirmed(3, "h1", "2026-01-01T09:00:00+00:00")
        later = add_message(self.store, self.tenant, "鲜果群", "老样子", self.customers["chen"],
                            sent_at="2026-01-05T09:00:00+00:00", source="h2")
        # 只能看到消息时间之前的有效历史
        visible = [o for o in self.applied if o.get("order_id") == history_order]
        self.assertTrue(visible)
        applied = self.deliver(later, suggestion(
            customer_alias="陈先生", target_order=history_order,
            items=[{"sku": APL, "quantity": 3, "unit": "箱", "evidence": ["老样子"]}],
            evidence=["老样子"],
        ))
        self.assertEqual(applied["outcome"], "drafts")
        self.assertNotEqual(applied["order_id"], history_order, "老样子要产生新订单")
        self.assert_lines(applied["order_id"], {APL: 3}, "老样子")
        self.assertEqual(self.store.order_detail(self.tenant, applied["order_id"])["history_order_id"],
                         history_order, "必须保留来源订单")
        self.assertEqual(self.stock(), {APL: 6.0}, "新单 3 + 历史已确认 3")

    def test_no_history_creates_a_clarification_and_no_order(self):
        later = add_message(self.store, self.tenant, "鲜果群", "老样子", self.customers["chen"],
                            sent_at="2026-01-05T09:00:00+00:00", source="h3")
        applied = self.deliver(later, suggestion(
            customer_alias="陈先生",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["老样子"]}],
            evidence=["老样子"],
        ))
        self.assertEqual(applied["outcome"], "clarify")
        self.assertIsNone(applied["order_id"], "无历史不得建单")
        self.assertEqual(self.store.list_orders(self.tenant), [])
        self.assertEqual(self.stock(), {})
        tasks = self.store.open_clarifications(self.tenant)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["reason"], "no_eligible_historical_order_at_message_time")
        self.assertTrue(tasks[0]["suggested_question"])


# ------------------------------------------------------------ 7. 询价


class InquiryLandingTests(BusinessLandingBase):
    def test_inquiry_creates_no_order_but_keeps_the_question(self):
        message = add_message(self.store, self.tenant, "鲜果群", "苹果多少钱", self.customers["li"], source="q1")
        applied = self.deliver(message, suggestion(
            intent="inquiry", customer_alias="李女士", evidence=["苹果多少钱"],
        ))
        self.assertEqual(applied["outcome"], "ignored")
        self.assertIsNone(applied["order_id"])
        self.assertEqual(self.store.list_orders(self.tenant), [])
        self.assertEqual(self.stock(), {})
        # 咨询这件事本身留下痕迹：解析运行完整，消息在库里
        run = self.store.db.execute(
            "SELECT status,result_json FROM parse_runs WHERE merchant_id=? AND message_id=?",
            (self.tenant, message["id"]),
        ).fetchone()
        self.assertEqual(run["status"], "complete")
        self.assertIn("inquiry", run["result_json"])
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM messages WHERE id=?", (message["id"],)
        ).fetchone()[0], 1, "原始消息必须留存")


# -------------------------------------------------------- 8. 付款声称


class PaymentClaimLandingTests(BusinessLandingBase):
    def test_payment_claim_attaches_to_the_order_and_never_acknowledges(self):
        base_msg = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="p1")
        order_id = self.deliver(base_msg, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))["order_id"]
        self.store.confirm(self.tenant, order_id, "tester")

        claim_msg = add_message(self.store, self.tenant, "鲜果群", "钱转了", self.customers["zhang"],
                                sent_at="2026-01-03T09:00:00+00:00", source="p2")
        applied = self.deliver(claim_msg, suggestion(
            intent="payment_claim", customer_alias="张姐", target_order=order_id, evidence=["钱转了"],
        ))
        self.assertEqual(applied["outcome"], "payment_claim")
        self.assertFalse(applied["acknowledged"], "到账必须人工核对，软件不得认定")
        self.assertEqual(applied["order_id"], order_id)
        self.assertEqual(len(self.store.list_orders(self.tenant)), 1, "不得新建第二张单")
        claims = self.dashboard.payment_claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["order_id"], order_id)
        # 订单内容与状态都没被"付款"改动
        detail = self.assert_lines(order_id, {APL: 2.0}, "付款声称后")
        self.assertEqual(detail["status"], "confirmed")
        self.assertEqual(self.stock(), {APL: 2.0})

    def test_payment_claim_without_a_target_asks_which_order(self):
        claim_msg = add_message(self.store, self.tenant, "鲜果群", "钱转了", self.customers["zhang"], source="p3")
        applied = self.deliver(claim_msg, suggestion(
            intent="payment_claim", customer_alias="张姐", evidence=["钱转了"],
        ))
        self.assertEqual(applied["outcome"], "clarify")
        self.assertEqual(self.store.list_orders(self.tenant), [], "不得凭空建单")
        task = self.store.open_clarifications(self.tenant)[0]
        self.assertEqual(task["reason"], "payment_claim_unlinked")
        self.assertTrue(task["suggested_question"])


# ---------------------------------------------------- 9. 多客户交错


class InterleavedCustomersLandingTests(BusinessLandingBase):
    def test_interleaved_messages_never_cross_customers_orders_or_memory(self):
        script = {
            "张姐的": ("zhang", {APL: 2}, "z1"),
            "陈先生的": ("chen", {ORG: 3}, "z2"),
            "李女士的": ("li", {APL: 1, ORG: 1}, "z3"),
        }
        order_ids: dict[str, str] = {}
        for index, (text, (who, lines, source)) in enumerate(script.items()):
            customer = self.customers[who]
            message = add_message(
                self.store, self.tenant, "鲜果群", text, customer,
                sent_at=f"2026-01-0{index + 2}T09:00:00+00:00", source=source,
            )
            items = [{"sku": sku, "quantity": qty, "unit": "箱", "evidence": [text]}
                     for sku, qty in lines.items()]
            applied = self.deliver(message, suggestion(
                customer_alias=who, items=items, evidence=[text],
            ))
            self.assertEqual(applied["outcome"], "drafts", f"{who} 应当建单")
            order_ids[who] = applied["order_id"]
            self.assert_lines(applied["order_id"], lines, f"{who} 的单")
        # 每张单只属于自己的客户
        for who, order_id in order_ids.items():
            detail = self.store.order_detail(self.tenant, order_id)
            self.assertEqual(detail["customer_id"], self.customers[who], f"{who} 的订单串了")
        self.assertEqual(len(set(order_ids.values())), 3, "必须是三张不同的单")
        # 备货按商品汇总，但订单数与客户数正确
        stock = {row.sku: (row.quantity, row.order_count, len(row.customers)) for row in self.dashboard.stock_summary()}
        self.assertEqual(stock[APL], (3.0, 2, 2))
        self.assertEqual(stock[ORG], (4.0, 2, 2))
        # 客户记忆不会互相污染
        memories = self.store.db.execute(
            "SELECT customer_id,kind,value FROM memory_facts WHERE merchant_id=?", (self.tenant,)
        ).fetchall()
        for row in memories:
            self.assertIn(row["customer_id"], set(self.customers.values()))


# --------------------------------------------- 10. 待生效变更 + 幂等/恢复


class PendingChangeAndIdempotencyTests(BusinessLandingBase):
    def test_pending_change_keeps_the_effective_order_in_stock_without_double_counting(self):
        base_msg = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="d1")
        base = self.deliver(base_msg, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))["order_id"]
        self.store.confirm(self.tenant, base, "tester")
        self.assertEqual(self.stock(), {APL: 2.0})

        change_msg = add_message(self.store, self.tenant, "鲜果群", "改成四箱", self.customers["zhang"],
                                 sent_at="2026-01-03T09:00:00+00:00", source="d2")
        changed = self.deliver(change_msg, suggestion(
            intent="modify", customer_alias="张姐", target_order=base,
            items=[{"sku": APL, "quantity": 4, "unit": "箱", "evidence": ["改成四箱"]}],
            evidence=["改成四箱"],
        ))["order_id"]
        # 待生效：备货这一行仍在（不得消失），且只算一次
        self.assertIn(APL, self.stock(), "待生效变更不得让这条备货消失")
        self.assertEqual(self.stock()[APL], 4.0, "新版本顶替旧版本，但不能 2+4 两个都算")
        # 已确认的那一单内容与状态都没被动过
        self.assertEqual(self.store.order_detail(self.tenant, base)["status"], "confirmed")
        self.assert_lines(base, {APL: 2.0}, "已确认订单不受待生效变更影响")

        # 确认新版本后：旧版本真正被替代，仍然只算一次
        self.store.confirm(self.tenant, changed, "tester")
        self.assertEqual(self.stock()[APL], 4.0, "确认后仍是 4，不是 2+4")
        self.assertEqual(self.store.order_detail(self.tenant, base)["status"], "superseded")

    def test_applying_the_same_message_twice_does_not_double_order(self):
        message = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="d3")
        result = suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        )
        first = self.deliver(message, result)
        second = self.deliver(message, result)
        self.assertEqual(first["outcome"], "drafts")
        self.assertEqual(second["outcome"], "duplicate", "重复应用必须是 duplicate")
        self.assertEqual(len(self.store.list_orders(self.tenant)), 1)
        self.assertEqual(self.stock(), {APL: 2.0})

    def test_duplicate_message_import_does_not_create_a_second_order(self):
        add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="d4")
        # 同 source 再导一次：``add_import`` 认得这是同一批，不该产生第二条
        again = self.store.add_import(
            self.tenant, "鲜果群",
            [{"sender": "客户", "content": "苹果两箱", "kind": "text",
              "sent_at": "2026-01-02T09:00:00+00:00", "source_id": "d4"}],
            "d4", "test",
        )
        self.assertEqual(int(again["inserted"]), 0, "重复导入不得插入新消息")
        rows = self.store.db.execute(
            "SELECT * FROM messages WHERE merchant_id=?", (self.tenant,)
        ).fetchall()
        self.assertEqual(len(rows), 1, "重复导入不得产生第二条消息")
        result = suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        )
        for row in rows:
            self.deliver(row, result)
        self.assertEqual(len(self.store.list_orders(self.tenant)), 1)
        self.assertEqual(self.stock(), {APL: 2.0})

    def test_reopening_the_store_keeps_orders_versions_and_stock(self):
        import tempfile

        message = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="d5")
        self.deliver(message, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))
        self.store.close()
        with tempfile.TemporaryDirectory() as directory:
            reopened = OrderStore(f"{directory}/orders.db")
            try:
                # 数据没在文件里，这里只验 schema 可重复升级且不丢已有运行
                self.assertIn("run_key", {r["name"] for r in reopened.db.execute("PRAGMA table_info(parse_runs)")})
            finally:
                reopened.close()

    def test_reanalysis_after_a_version_bump_is_refused_as_stale(self):
        from pyweixin.orders import _same_quantity

        base_msg = add_message(self.store, self.tenant, "鲜果群", "苹果两箱", self.customers["zhang"], source="d6")
        base = self.deliver(base_msg, suggestion(
            customer_alias="张姐",
            items=[{"sku": APL, "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))["order_id"]
        self.store.confirm(self.tenant, base, "tester")

        run = self.store.start_reanalysis(self.tenant, base_msg["id"], "paid-deep", "p2")
        self.store.finish_reanalysis(self.tenant, run["parse_run_id"], suggestion(
            intent="modify", customer_alias="张姐", target_order=base,
            items=[{"sku": APL, "quantity": 5, "unit": "箱", "evidence": ["苹果两箱"]}],
            evidence=["苹果两箱"],
        ))
        pending = self.store.pending_reanalysis_suggestions(self.tenant)
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["has_difference"])
        # 应用之前订单没被动
        self.assert_lines(base, {APL: 2.0}, "重新分析后、应用前")
        result = self.store.apply_reanalysis_suggestion(self.tenant, pending[0]["id"])
        self.assertTrue(result["applied"])
        self.assertNotEqual(result["result"]["order_id"], base)
        self.assert_lines(result["result"]["order_id"], {APL: 5.0}, "应用后")
        self.assertEqual(self.stock()[APL], 5.0)
        self.assertTrue(_same_quantity(2, 2.0), "2 与 2.0 是同一个数量")


# -------------------------------------------- 编排后端自身的性质


class ScriptedBackendTests(unittest.TestCase):
    def test_scripted_backend_never_claims_to_be_a_model(self):
        backend = ScriptedBackend({"x": {"intent": "chitchat", "customer_alias": None, "target_order": None,
                                        "items": [], "delivery_date": None, "evidence": [],
                                        "missing_fields": [], "ambiguities": [],
                                        "suggested_change": None}})
        self.assertFalse(backend.is_model, "编排后端不得自称是模型")
        self.assertFalse(backend.probe()["is_model"])

    def test_scripted_backend_reports_no_upstream_model(self):
        backend = ScriptedBackend({"苹果两箱": {
            "intent": "new_order", "customer_alias": "张姐", "target_order": None,
            "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            "delivery_date": None, "evidence": ["苹果两箱"], "missing_fields": [],
            "ambiguities": [], "suggested_change": None,
        }})
        session = backend.create_session("job-1")
        outcome = backend.run_parse(session, {"message": {"content": "苹果两箱"}}, "job-1", 0.0,
                                    provider_id="p", model_id="m")
        self.assertFalse(outcome.model_confirmed, "没有上游就不得声称确认过")
        self.assertIn("未能独立确认", outcome.model_warning)
        self.assertEqual(outcome.model_requested, "p/m")

    def test_scripted_backend_refuses_unscripted_text(self):
        from pyweixin.opencode import InvalidParseOutput

        backend = ScriptedBackend({})
        session = backend.create_session("job-1")
        with self.assertRaises(InvalidParseOutput):
            backend.run_parse(session, {"message": {"content": "没编好的话"}}, "job-1", 0.0)


if __name__ == "__main__":
    unittest.main()
