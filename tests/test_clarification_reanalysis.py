"""独立澄清任务、换模型差异流程、计费唯一来源、路由核实。

每条测试都对应本轮确定的产品决策，或一个具体缺陷：

* 意图不明确 → 独立澄清任务，**不建空订单**；
* 换模型重新分析 → 出差异建议，应用前比版本，重复应用不重复改；
* 网关是唯一计费来源 → 客户端不再自己算扣费，价格变了要求重新确认；
* 能力验证与正式任务走**同一套**路由传递机制，并核对上游实际执行的模型。
"""
from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.client_login import GatewayError  # noqa: E402
from pyweixin.orders import OrderStore  # noqa: E402

VALID = {
    "intent": "new_order", "customer_alias": "张姐", "target_order": None,
    "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["来两箱苹果"]}],
    "delivery_date": None, "evidence": ["来两箱苹果"], "missing_fields": [],
    "ambiguities": [], "suggested_change": None,
}


def new_store() -> tuple[OrderStore, str, str, str]:
    store = OrderStore(":memory:")
    tenant = store.create_merchant("测试店", plan="demo", ai_quota=0)["merchant_id"]
    customer = store.add_customer(tenant, "张姐", "张姐", "群A")
    store.add_product(tenant, "APL", "苹果", "箱", 2600, ["苹果"])
    store.add_product(tenant, "ORG", "橙子", "箱", 2200, ["橙子"])
    return store, tenant, customer, "APL"


def add_message(store, tenant, group, content, customer_id=None, sent_at="2026-01-02T09:00:00+00:00", source="s1"):
    store.add_import(
        tenant, group,
        [{"sender": "张姐" if customer_id else "路人", "content": content, "kind": "text",
          "sent_at": sent_at, "source_id": source}],
        source=source, source_type="test",
    )
    row = store.db.execute(
        "SELECT * FROM messages WHERE merchant_id=? AND source_id=?", (tenant, source)
    ).fetchone()
    if customer_id:
        store.bind_message_customer(tenant, row["id"], customer_id, "tester")
        row = store.db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
    return row


# --------------------------------------------------------------- 澄清任务


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tenant, self.customer, self.sku = new_store()
        self.add = self.store.add_clarification_message if False else None

    def tearDown(self):
        self.store.close()

    def msg(self, content, customer=None, sent_at="2026-01-02T09:00:00+00:00", source="s1", sender=None):
        row = add_message(self.store, self.tenant, "群A", content, customer, sent_at, source)
        if sender is not None:
            self.store.db.execute("UPDATE messages SET sender=? WHERE id=?", (sender, row["id"]))
            self.store.db.commit()
            row = self.store.db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
        return row

    def test_unclear_creates_a_clarification_and_no_order(self):
        message = self.msg("来两箱那个", self.customer, source="c1")
        applied = self.store.apply_cloud_suggestion(
            self.tenant, "群A", message["id"],
            {**VALID, "intent": "unclear", "items": [], "evidence": ["来两箱那个"],
             "missing_fields": ["product"]},
        )
        self.assertEqual(applied["outcome"], "clarify")
        self.assertIsNone(applied["order_id"])
        self.assertEqual(self.store.list_orders(self.tenant), [], "不得建空订单")
        tasks = self.store.open_clarifications(self.tenant)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["reason"], "intent_unclear")
        self.assertEqual(tasks[0]["message_id"], message["id"])
        self.assertEqual(tasks[0]["missing_fields"], ["product"])
        self.assertTrue(tasks[0]["suggested_question"])
        self.assertTrue(tasks[0]["parse_run_id"], "来源解析运行必须留痕")
        self.assertFalse(tasks[0]["counts_as_order"])

    def test_missing_target_order_asks_instead_of_creating_a_new_order(self):
        first = self.msg("来两箱苹果", self.customer, source="m1")
        self.store.apply_cloud_suggestion(self.tenant, "群A", first["id"], VALID)
        second = self.msg("再加一箱", self.customer, sent_at="2026-01-03T09:00:00+00:00", source="m2")
        applied = self.store.apply_cloud_suggestion(
            self.tenant, "群A", second["id"],
            {**VALID, "intent": "append", "target_order": None,
             "items": [{"sku": "APL", "quantity": 1, "unit": "箱", "evidence": ["再加一箱"]}],
             "evidence": ["再加一箱"]},
        )
        self.assertEqual(applied["outcome"], "clarify")
        self.assertIsNone(applied["order_id"], "不知道给哪单加，就不能建新单")
        self.assertEqual(len(self.store.list_orders(self.tenant)), 1, "原有订单不受影响")
        self.assertEqual(
            self.store.open_clarifications(self.tenant)[0]["reason"], "target_order_unresolved"
        )

    def test_replaying_the_same_message_does_not_duplicate_the_clarification(self):
        """重试、重复导入、回放都不能把待办翻倍。"""
        message = self.msg("来两箱那个", self.customer, source="c2")
        suggestion = {**VALID, "intent": "unclear", "items": [], "evidence": ["来两箱那个"]}
        first = self.store.apply_cloud_suggestion(self.tenant, "群A", message["id"], suggestion)
        again = self.store.apply_cloud_suggestion(self.tenant, "群A", message["id"], suggestion)
        self.assertEqual(first["outcome"], "clarify")
        self.assertEqual(len(self.store.open_clarifications(self.tenant)), 1)
        self.assertIn(again["outcome"], {"clarify", "duplicate"})

    def test_a_following_message_with_enough_info_closes_the_clarification(self):
        vague = self.msg("来两箱那个", self.customer, source="c3")
        self.store.apply_cloud_suggestion(
            self.tenant, "群A", vague["id"],
            {**VALID, "intent": "unclear", "items": [], "evidence": ["来两箱那个"]},
        )
        self.assertEqual(len(self.store.open_clarifications(self.tenant)), 1)
        clear = self.msg("苹果两箱", self.customer, sent_at="2026-01-03T10:00:00+00:00", source="c4")
        applied = self.store.apply_cloud_suggestion(
            self.tenant, "群A", clear["id"],
            {**VALID, "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
             "evidence": ["苹果两箱"]},
        )
        self.assertEqual(applied["outcome"], "drafts")
        self.assertTrue(applied["order_id"])
        self.assertEqual(self.store.open_clarifications(self.tenant), [], "信息齐了就得关掉")
        task = self.store.clarification(
            self.tenant,
            self.store.db.execute(
                "SELECT id FROM clarification_tasks WHERE merchant_id=?", (self.tenant,)
            ).fetchone()["id"],
        )
        self.assertEqual(task["status"], "resolved")
        self.assertEqual(task["resolved_by_message_id"], clear["id"])
        self.assertEqual(task["resolved_order_id"], applied["order_id"])

    def test_multiple_open_clarifications_are_never_guessed_which_one(self):
        """多个候选时不任意关联：猜错等于抹掉别的客户的待办。"""
        first = self.msg("来两箱那个", self.customer, source="c5")
        second = self.msg("还是那个", self.customer, sent_at="2026-01-02T10:00:00+00:00", source="c6")
        vague = {**VALID, "intent": "unclear", "items": [], "evidence": []}
        self.store.apply_cloud_suggestion(self.tenant, "群A", first["id"], vague)
        self.store.apply_cloud_suggestion(
            self.tenant, "群A", second["id"], {**vague, "evidence": ["还是那个"]}
        )
        self.assertEqual(len(self.store.open_clarifications(self.tenant)), 2)
        clear = self.msg("苹果两箱", self.customer, sent_at="2026-01-04T10:00:00+00:00", source="c7")
        self.store.apply_cloud_suggestion(
            self.tenant, "群A", clear["id"],
            {**VALID, "items": [{"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
             "evidence": ["苹果两箱"]},
        )
        self.assertEqual(len(self.store.open_clarifications(self.tenant)), 2,
                         "两个候选时不得替客户选")

    def test_clarifications_do_not_pollute_order_or_stock_statistics(self):
        message = self.msg("来两箱那个", self.customer, source="c8")
        self.store.apply_cloud_suggestion(
            self.tenant, "群A", message["id"],
            {**VALID, "intent": "unclear", "items": [], "evidence": ["来两箱那个"]},
        )
        from pyweixin.dashboard import DashboardBuilder

        builder = DashboardBuilder(self.store, self.tenant)
        self.assertEqual(builder.stock_summary(), [], "澄清任务不是备货")
        self.assertEqual(self.store.dashboard(self.tenant)["draft_orders"], 0)
        self.assertEqual(len(builder.clarifications()), 1)
        # 异常计数包含它，但订单数不包含。
        self.assertEqual(builder.exception_count(), 1)
        self.assertEqual(len(builder.effective_orders()), 0)

    def test_clarification_keeps_the_original_message_and_parse_record(self):
        message = self.msg("来两箱那个", self.customer, source="c9")
        self.store.apply_cloud_suggestion(
            self.tenant, "群A", message["id"],
            {**VALID, "intent": "unclear", "items": [], "evidence": ["来两箱那个"]},
        )
        runs = self.store.db.execute(
            "SELECT id,status FROM parse_runs WHERE merchant_id=? AND message_id=?",
            (self.tenant, message["id"]),
        ).fetchall()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "complete", "解析记录必须留存")
        self.assertIsNotNone(
            self.store.db.execute("SELECT id FROM messages WHERE id=?", (message["id"],)).fetchone()
        )


# ---------------------------------------------------------- 占位订单迁移


class PlaceholderMigrationTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tenant, self.customer, _sku = new_store()

    def tearDown(self):
        self.store.close()

    def _make_placeholder(self, reason: str, with_items: bool = False, status: str = "review",
                         tag: str = "") -> str:
        """造一条旧版本留下的空订单占位。"""
        key = tag or reason
        message = add_message(self.store, self.tenant, "群A", "来两箱那个", self.customer, source=f"m-{key}")
        order_id = "o_legacy_" + key
        now = self.store.now()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO orders(id,merchant_id,customer_id,group_id,status,delivery_date,source_import_id,"
                "created_at,updated_at,version,history_order_id,claim_payment,change_type,business_at_epoch,parse_run_id)"
                " VALUES(?,?,?,'群A',?,NULL,NULL,?,?,1,NULL,0,'new',NULL,NULL)",
                (order_id, self.tenant, self.customer, status, now, now),
            )
            if with_items:
                product = self.store.db.execute(
                    "SELECT * FROM products WHERE merchant_id=? AND sku='APL'", (self.tenant,)
                ).fetchone()
                self.store.db.execute(
                    "INSERT INTO order_items VALUES(?,?,?,?,?,?)",
                    ("oi_" + key, order_id, product["id"], 2.0, "箱", 2600),
                )
            self.store.db.execute(
                "INSERT INTO review_tasks VALUES(?,?,?,?,'open')",
                ("rt_" + key, self.tenant, order_id, reason),
            )
            self.store.db.execute(
                "INSERT INTO evidence VALUES(?,?,?,?, 'source')",
                ("ev_" + key, self.tenant, order_id, message["id"]),
            )
        return order_id

    def test_placeholder_becomes_a_clarification_without_deleting_the_order(self):
        order_id = self._make_placeholder("product_or_quantity_unresolved")
        report = self.store.migrate_clarification_placeholders()
        self.assertEqual(report["moved"], 1)
        self.assertEqual(report["details"][0]["clarification_id"] != "", True)
        tasks = self.store.open_clarifications(self.tenant)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["order_id"], order_id, "要保留原订单的关联")
        # 订单没被删，只是标记为被替代。
        row = self.store.db.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM evidence WHERE order_id=?", (order_id,)).fetchone()[0], 1,
            "证据必须保留",
        )

    def test_real_orders_are_never_migrated(self):
        with_items = self._make_placeholder("product_or_quantity_unresolved", with_items=True, tag="items")
        confirmed = self._make_placeholder("product_or_quantity_unresolved", status="confirmed", tag="conf")
        cancel_review = self._make_placeholder("product_or_quantity_unresolved", status="cancel_review", tag="cxl")
        retry_task = self._make_placeholder("model_parse_failed", tag="retry")
        report = self.store.migrate_clarification_placeholders()
        self.assertEqual(report["moved"], 0, "有明细/已确认/取消待核对/系统重试的单一律不动")
        for order_id in (with_items, confirmed, cancel_review, retry_task):
            self.assertNotEqual(
                self.store.db.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()["status"],
                "superseded",
            )

    def test_migration_is_repeatable(self):
        self._make_placeholder("intent_unclear")
        self.assertEqual(self.store.migrate_clarification_placeholders()["moved"], 1)
        self.assertEqual(self.store.migrate_clarification_placeholders()["moved"], 0)
        self.assertEqual(len(self.store.open_clarifications(self.tenant)), 1)

    def test_dry_run_changes_nothing(self):
        self._make_placeholder("intent_unclear")
        report = self.store.migrate_clarification_placeholders(dry_run=True)
        self.assertEqual(report["moved"], 1)
        self.assertEqual(self.store.open_clarifications(self.tenant), [])
        self.assertEqual(
            self.store.db.execute("SELECT status FROM orders").fetchone()["status"], "review"
        )


# ------------------------------------------------------------ 重新分析


class ReanalysisDiffTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tenant, self.customer, _sku = new_store()
        self.message = add_message(self.store, self.tenant, "群A", "来两箱苹果", self.customer, source="r1")
        self.applied = self.store.apply_cloud_suggestion(self.tenant, "群A", self.message["id"], VALID)
        self.order_id = self.applied["order_id"]

    def tearDown(self):
        self.store.close()

    def _reanalyze(self, suggestion, model="paid-deep"):
        run = self.store.start_reanalysis(self.tenant, self.message["id"], model, "p2")
        return run, self.store.finish_reanalysis(self.tenant, run["parse_run_id"], suggestion)

    def test_no_difference_is_reported_explicitly(self):
        _run, suggestion_id = self._reanalyze(VALID)
        view = self.store.reanalysis_suggestion(self.tenant, suggestion_id)
        self.assertFalse(view["has_difference"])
        self.assertEqual(view["status"], "applied")
        self.assertEqual(view["outcome"], "no_difference")
        self.assertEqual(len(self.store.list_orders(self.tenant)), 1, "无差异不得改动订单")

    def test_quantity_difference_is_reported_not_applied(self):
        changed = {**VALID, "items": [{"sku": "APL", "quantity": 3, "unit": "箱", "evidence": ["来三箱苹果"]}],
                   "evidence": ["来三箱苹果"]}
        _run, suggestion_id = self._reanalyze(changed)
        view = self.store.reanalysis_suggestion(self.tenant, suggestion_id)
        self.assertTrue(view["has_difference"])
        self.assertEqual(view["status"], "pending", "有待应用的建议，不得自动生效")
        self.assertEqual(view["diff"]["items_changed"][0]["from"]["quantity"], 2)
        self.assertEqual(view["diff"]["items_changed"][0]["to"]["quantity"], 3)
        # 订单没被动过
        self.assertEqual(len(self.store.list_orders(self.tenant)), 1)
        self.assertEqual(
            float(self.store.order_detail(self.tenant, self.order_id)["items"][0]["quantity"]), 2.0
        )

    def test_adding_and_removing_items_are_reported(self):
        added = {**VALID, "items": [
            {"sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["来两箱苹果"]},
            {"sku": "ORG", "quantity": 1, "unit": "箱", "evidence": ["来两箱苹果"]},
        ]}
        _run, sid = self._reanalyze(added)
        self.assertEqual(self.store.reanalysis_suggestion(self.tenant, sid)["diff"]["items_added"], ["ORG"])
        removed = {**VALID, "items": []}
        _run, sid2 = self._reanalyze(removed)
        self.assertEqual(self.store.reanalysis_suggestion(self.tenant, sid2)["diff"]["items_removed"], ["APL"])

    def test_delivery_date_difference_is_reported(self):
        _run, sid = self._reanalyze({**VALID, "delivery_date": "2026-01-05"})
        diff = self.store.reanalysis_suggestion(self.tenant, sid)["diff"]
        self.assertTrue(diff["delivery_date_changed"])

    def test_applying_a_suggestion_creates_a_new_reviewable_version(self):
        changed = {**VALID, "items": [{"sku": "APL", "quantity": 3, "unit": "箱", "evidence": ["来三箱苹果"]}],
                   "evidence": ["来三箱苹果"]}
        _run, sid = self._reanalyze(changed)
        result = self.store.apply_reanalysis_suggestion(self.tenant, sid)
        self.assertTrue(result["applied"])
        new_id = result["result"]["order_id"]
        self.assertNotEqual(new_id, self.order_id, "已生效订单不得就地改")
        self.assertEqual(float(self.store.order_detail(self.tenant, self.order_id)["items"][0]["quantity"]), 2.0)
        self.assertEqual(float(self.store.order_detail(self.tenant, new_id)["items"][0]["quantity"]), 3.0)

    def test_applying_the_same_suggestion_twice_changes_nothing(self):
        changed = {**VALID, "items": [{"sku": "APL", "quantity": 4, "unit": "箱", "evidence": ["来四箱苹果"]}],
                   "evidence": ["来四箱苹果"]}
        _run, sid = self._reanalyze(changed)
        self.store.apply_reanalysis_suggestion(self.tenant, sid)
        before = len(self.store.list_orders(self.tenant))
        again = self.store.apply_reanalysis_suggestion(self.tenant, sid)
        self.assertFalse(again["applied"])
        self.assertEqual(again["reason"], "already_applied")
        self.assertEqual(len(self.store.list_orders(self.tenant)), before, "不得重复落单")

    def test_stale_base_version_is_refused(self):
        """订单在比较之后变了：必须重新比较，不能拿旧建议覆盖。"""
        changed = {**VALID, "items": [{"sku": "APL", "quantity": 5, "unit": "箱", "evidence": ["来五箱苹果"]}],
                   "evidence": ["来五箱苹果"]}
        _run, sid = self._reanalyze(changed)
        # 客户自己又改了一单 → 版本前进
        later = add_message(self.store, self.tenant, "群A", "改成六箱", self.customer,
                            sent_at="2026-01-03T09:00:00+00:00", source="r2")
        self.store.apply_cloud_suggestion(
            self.tenant, "群A", later["id"],
            {**VALID, "intent": "modify", "target_order": self.order_id,
             "items": [{"sku": "APL", "quantity": 6, "unit": "箱", "evidence": ["改成六箱"]}],
             "evidence": ["改成六箱"]},
        )
        result = self.store.apply_reanalysis_suggestion(self.tenant, sid)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "stale_base_version")
        self.assertEqual(self.store.reanalysis_suggestion(self.tenant, sid)["status"], "stale")
        self.assertEqual(len(self.store.pending_reanalysis_suggestions(self.tenant)), 0)

    def test_dismissed_suggestion_is_recorded_and_applies_nothing(self):
        changed = {**VALID, "items": [{"sku": "APL", "quantity": 9, "unit": "箱", "evidence": ["来九箱苹果"]}],
                   "evidence": ["来九箱苹果"]}
        _run, sid = self._reanalyze(changed)
        view = self.store.dismiss_reanalysis_suggestion(self.tenant, sid, "不采纳")
        self.assertEqual(view["status"], "dismissed")
        self.assertEqual(view["outcome"], "不采纳")
        again = self.store.apply_reanalysis_suggestion(self.tenant, sid)
        self.assertFalse(again["applied"])
        self.assertEqual(len(self.store.list_orders(self.tenant)), 1)

    def test_reanalysis_does_not_disturb_the_automatic_run(self):
        automatic = self.store.db.execute(
            "SELECT id,status,run_key,requested_by FROM parse_runs WHERE merchant_id=? AND run_key=''",
            (self.tenant,),
        ).fetchall()
        self.assertEqual(len(automatic), 1)
        run = self.store.start_reanalysis(self.tenant, self.message["id"], "paid-deep", "p2")
        after = self.store.db.execute(
            "SELECT id,status,run_key,requested_by FROM parse_runs WHERE merchant_id=? AND run_key=''",
            (self.tenant,),
        ).fetchall()
        self.assertEqual(len(after), 1, "自动运行不受影响")
        self.assertEqual(after[0]["id"], automatic[0]["id"])
        detail = self.store.db.execute(
            "SELECT requested_by,model,prompt_version FROM parse_runs WHERE id=?", (run["parse_run_id"],)
        ).fetchone()
        self.assertEqual(detail["requested_by"], "merchant")
        self.assertEqual(detail["model"], "paid-deep")
        self.assertEqual(detail["prompt_version"], "p2")

    def test_multiple_runs_of_the_same_message_are_allowed(self):
        for index in range(3):
            self.store.start_reanalysis(self.tenant, self.message["id"], f"m{index}", "p1")
        self.store.db.commit()
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM parse_runs WHERE merchant_id=? AND run_key!=''", (self.tenant,)
            ).fetchone()[0],
            3,
        )

    def test_an_automatic_run_cannot_be_finished_as_a_reanalysis(self):
        automatic = self.store.db.execute(
            "SELECT id FROM parse_runs WHERE merchant_id=? AND run_key=''", (self.tenant,)
        ).fetchone()["id"]
        with self.assertRaises(ValueError):
            self.store.finish_reanalysis(self.tenant, automatic, VALID)


# ---------------------------------------------------------- 计费唯一来源


class BillingSourceTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        from pyweixin.gateway import ProductGateway
        from pyweixin.model_catalog import (
            CatalogStatus, CatalogStore, OperatorModelPolicy, PricingClass, build_catalog,
        )
        from pyweixin.opencode import LocalRuleAdapter
        from pyweixin.product import ProductStore

        self.dir = tempfile.TemporaryDirectory()
        self.store = ProductStore(":memory:")
        policies = {
            "free-standard": OperatorModelPolicy(
                alias="free-standard", provider_id="p", model_id="m-free",
                pricing_class=PricingClass.METERED, daily_free_limit=0, is_default=True,
            ),
            "paid-deep": OperatorModelPolicy(
                alias="paid-deep", provider_id="p", model_id="m-paid",
                pricing_class=PricingClass.METERED, cost_units_per_job=3,
            ),
        }
        catalog = CatalogStore()
        catalog.load_policies(list(policies.values()))
        catalog.catalog = build_catalog(
            [{"provider_id": "p", "name": "p", "connected": True, "models": [
                {"model_id": "m-free", "display_name": "F", "context_limit": 1, "output_limit": 1,
                 "capabilities": {}, "upstream_cost": {}, "upstream_status": "live"},
                {"model_id": "m-paid", "display_name": "P", "context_limit": 1, "output_limit": 1,
                 "capabilities": {}, "upstream_cost": {}, "upstream_status": "live"},
            ]}], policies,
        )
        for alias in policies:
            catalog.mark_status(alias, CatalogStatus.SELECTABLE, verified=True, now=1.0)
        self.gateway = ProductGateway(store=self.store, adapter=LocalRuleAdapter(), secure_cookies=False, catalog=catalog)
        self.pricing_version = self.gateway.catalog.for_client()["version"]
        self.account = self.store.create_account(
            "shop", "password-1234", display_name="店", quota_units=50, max_concurrent_jobs=4
        )
        self.device_id, _ = self.store._upsert_device(
            self.account["account_id"], self.account["merchant_id"], "pc"
        )
        self.identity = {
            "account_id": self.account["account_id"], "device_id": self.device_id,
            "merchant_id": self.account["merchant_id"],
        }

    def tearDown(self):
        self.dir.cleanup()

    def _submit(self, key, payload):
        body = dict(payload)
        body.setdefault("pricing_version", self.pricing_version)
        return self.gateway._create_job(self.identity, body, {"Idempotency-Key": key})

    def _payload(self, **extra):
        payload = {
            "message": {"id": "m1", "sender": "张姐", "content": "来两箱苹果", "kind": "text",
                        "sent_at": "2026-01-01T10:00:00+08:00"},
            "customer": {"id": "c1", "name": "张姐", "alias": "张姐", "address": ""},
            "products": [{"sku": "APL", "name": "苹果", "unit": "箱", "aliases": "苹果"}],
            "history": {"recent_messages": [], "customer_facts": [], "recent_orders": []},
            "order_state": {},
        }
        payload.update(extra)
        return payload

    def test_job_response_carries_machine_readable_billing(self):
        response = self._submit("b1", self._payload(model="paid-deep"))
        billing = response.payload["billing"]
        self.assertTrue(billing["available"])
        self.assertEqual(billing["pricing_class"], "metered")
        self.assertEqual(billing["cost_units_per_job"], 3, "服务端口径必须下发")
        self.assertEqual(billing["charged_units"], 0)
        self.assertEqual(billing["reserved_units"], 3)
        self.assertTrue(billing["policy_version"])
        self.assertTrue(billing["charge_line"])

    def test_stale_pricing_version_is_refused_and_asks_for_reconfirmation(self):
        """策略在商户确认之后变了：必须重新确认，不能按新价悄悄扣。"""
        with self.assertRaises(Exception) as caught:
            self._submit("b3", self._payload(model="paid-deep", pricing_version="cat_stale"))
        error = caught.exception
        self.assertEqual(error.code, "pricing_changed")
        self.assertEqual(error.http_status, 409)
        self.assertEqual(error.facts["cost_units_per_job"], 3)
        self.assertEqual(self.store.list_jobs(self.account["account_id"]), [], "不得建任务")

    def test_matching_pricing_version_is_accepted(self):
        first = self._submit("b4", self._payload(model="paid-deep"))
        version = first.payload["billing"]["policy_version"]
        self._submit("b5", self._payload(model="paid-deep", pricing_version=version))
        self.assertEqual(len(self.store.list_jobs(self.account["account_id"])), 2)

    def test_paid_job_without_a_pricing_version_is_refused(self):
        """收费任务必须带有效版本：没有版本就没有"商户确认过的那个价"。

        缺失时不建任务、不预留、不执行 —— 按当前价格扣就是不知情收费。
        """
        with self.assertRaises(Exception) as caught:
            self._submit("b-nover", self._payload(model="paid-deep", pricing_version=""))
        error = caught.exception
        self.assertEqual(error.code, "pricing_version_required")
        self.assertEqual(error.http_status, 409)
        self.assertEqual(error.facts["cost_units_per_job"], 3, "要把当前事实告诉客户端去展示")
        self.assertEqual(self.store.list_jobs(self.account["account_id"]), [], "不得建任务")
        self.assertEqual(
            self.store.get_subscription(self.account["account_id"])["reserved_units"], 0, "不得预留"
        )

    def test_free_turned_paid_cannot_slip_through_without_a_version(self):
        """免费模型变成收费后，"没带版本"的老请求不能静默按收费继续。"""
        policies = {p.alias: p for p in self.gateway.catalog.policies()}
        free_policy = policies["free-standard"]
        import dataclasses

        entry = self.gateway.catalog.entry("free-standard")
        entry.policy = dataclasses.replace(
            free_policy, pricing_class="metered", daily_free_limit=0, cost_units_per_job=2
        )
        self.gateway.catalog.catalog.version = self.gateway.catalog.catalog.compute_version()
        self.pricing_version = self.gateway.catalog.catalog.version
        # 带着**旧**版本（免费时那一版）来 → pricing_changed
        with self.assertRaises(Exception) as changed:
            self._submit("b-swap", self._payload(model="free-standard", pricing_version="cat_old"))
        self.assertEqual(changed.exception.code, "pricing_changed")
        # 完全不带版本 → pricing_version_required
        with self.assertRaises(Exception) as missing:
            self._submit("b-swap2", self._payload(model="free-standard", pricing_version=""))
        self.assertEqual(missing.exception.code, "pricing_version_required")
        self.assertEqual(self.store.list_jobs(self.account["account_id"]), [])

    def test_idempotent_replay_returns_the_original_job_and_original_charge(self):
        """重试一笔已存在的任务：返回原任务与原计费，**不按新价重收**。"""
        first = self._submit("b-replay", self._payload(model="paid-deep"))
        job_id = first.payload["job"]["job_id"]
        self.assertEqual(first.payload["billing"]["cost_units_per_job"], 3)
        self.gateway.start(workers=1)
        try:
            import time

            deadline = time.time() + 15
            while time.time() < deadline:
                if self.store.get_job(self.account["account_id"], job_id)["state"] == "succeeded":
                    break
                time.sleep(0.05)
        finally:
            self.gateway.stop()
        used_before = self.store.get_subscription(self.account["account_id"])["used_units"]
        self.assertEqual(used_before, 3)

        # 价格翻倍之后重试同一个幂等键
        self.gateway.catalog.entry("paid-deep").policy = dataclasses.replace(
            self.gateway.catalog.entry("paid-deep").policy, cost_units_per_job=9
        )
        self.gateway.catalog.catalog.version = self.gateway.catalog.catalog.compute_version()
        replay = self._submit("b-replay", self._payload(model="paid-deep"))
        self.assertTrue(replay.payload.get("replayed"))
        self.assertEqual(replay.payload["job"]["job_id"], job_id, "必须返回原任务")
        self.assertEqual(replay.payload["billing"]["cost_units_per_job"], 3, "播报冻结时的口径")
        self.assertEqual(
            self.store.get_subscription(self.account["account_id"])["used_units"], used_before,
            "重试不得按新价重新收费",
        )

    def test_same_key_different_request_still_conflicts(self):
        """幂等机制不能被用来换模型或换正文。"""
        self._submit("b-conflict", self._payload(model="paid-deep"))
        with self.assertRaises(Exception) as caught:
            self._submit("b-conflict", self._payload(model="free-standard"))
        self.assertEqual(caught.exception.code, "idempotency_key_reuse")

    def test_client_never_computes_the_charge_itself(self):
        """客户端拿到的必须是服务端数字；本地没有第二套算法。"""
        from pyweixin.local_parse import LocalParseService

        source = source_of(LocalParseService)
        # 服务端字段被读取并原样转述…
        for field in ("body.get(\"billing\")", "fetched.get(\"billing\")", "def billing_fact"):
            self.assertIn(field, source, f"客户端必须读取服务端计费字段：{field}")
        # …而本地没有任何一处"每个任务扣 1 次"的推算。
        self.assertNotIn("charged += 1", source)
        self.assertNotIn("cost_units=1", source)
        self.assertNotIn("cost_units_per_job=", source, "客户端不得自己读策略算钱")

    def test_wizard_summary_aggregates_server_numbers_only(self):
        import inspect

        from pyweixin.wizard import Wizard

        source = inspect.getsource(Wizard.analyze)
        self.assertIn("billing_fact", source, "汇总必须来自服务端事实")
        self.assertIn('fact.get("charged_units", 0)', source)
        self.assertNotIn("charged += 1", source)

    def test_gateway_error_carries_billing_facts(self):
        from pyweixin.client_login import GatewayError

        error = GatewayError("pricing_changed", "变了", 409, billing={"cost_units_per_job": 3})
        self.assertEqual(error.billing["cost_units_per_job"], 3)


def source_of(obj) -> str:
    import inspect

    return inspect.getsource(obj)


# ------------------------------------------------ 路由：请求 vs 实际模型


class UpstreamModelIdentityTests(unittest.TestCase):
    """能力验证与正式任务必须走同一套路由，并且**核对**上游实际跑的是哪个模型。"""

    def _adapter(self, replies, requested=("opencode-go", "qwen3.6-plus"), returned=None):
        from pyweixin.opencode import OpenCodeAdapter

        seen: list[dict] = []
        returned = returned or requested

        def transport(method, path, body, timeout):
            del timeout
            if path == "/session":
                return {"id": "ses-1"}
            seen.append(dict(body or {}))
            return {
                "info": {"id": "m1", "role": "assistant",
                         "modelID": returned[1], "providerID": returned[0]},
                "parts": [{"type": "text", "text": json.dumps(replies, ensure_ascii=False)}],
            }

        adapter = OpenCodeAdapter("http://127.0.0.1:1", password="p")
        adapter._backend._transport = transport
        return adapter, seen

    def test_request_body_names_the_model_and_the_reply_confirms_it(self):
        adapter, seen = self._adapter({"intent": "unclear", "customer_alias": None, "target_order": None,
                                       "items": [], "delivery_date": None, "evidence": [],
                                       "missing_fields": ["x"], "ambiguities": ["y"],
                                       "suggested_change": None})
        outcome = adapter.run_parse("ses-1", {"message": {}}, "job-1", 0.0,
                                    provider_id="opencode-go", model_id="qwen3.6-plus")
        self.assertEqual(seen[0]["model"], {"providerID": "opencode-go", "modelID": "qwen3.6-plus"})
        self.assertEqual(outcome.model_requested, "opencode-go/qwen3.6-plus")
        self.assertEqual(outcome.model_reported, "opencode-go/qwen3.6-plus")
        self.assertTrue(outcome.model_confirmed)
        self.assertEqual(outcome.model_warning, "")

    def test_a_mismatched_upstream_model_is_recorded_not_silently_accepted(self):
        """上游跑了别的模型：记录明确错误，**不算**指定模型验证成功。"""
        adapter, _seen = self._adapter(
            {"intent": "unclear", "customer_alias": None, "target_order": None, "items": [],
             "delivery_date": None, "evidence": [], "missing_fields": ["x"],
             "ambiguities": ["y"], "suggested_change": None},
            requested=("opencode-go", "qwen3.6-plus"),
            returned=("some-other-provider", "some-other-model"),
        )
        outcome = adapter.run_parse("ses-1", {"message": {}}, "job-1", 0.0,
                                    provider_id="opencode-go", model_id="qwen3.6-plus")
        self.assertEqual(outcome.model_reported, "some-other-provider/some-other-model")
        self.assertFalse(outcome.model_confirmed)
        self.assertIn("不一致", outcome.model_warning)

    def test_missing_upstream_model_identity_is_reported_as_unconfirmed(self):
        """上游没回模型标识时标「未能独立确认」，**不编造**一致性证据。"""
        from pyweixin.opencode import OpenCodeAdapter

        seen: list[dict] = []

        def transport(method, path, body, timeout):
            del timeout
            if path == "/session":
                return {"id": "ses-1"}
            seen.append(dict(body or {}))
            return {"info": {"id": "m1", "role": "assistant"},
                    "parts": [{"type": "text", "text": json.dumps(
                        {"intent": "unclear", "customer_alias": None, "target_order": None,
                         "items": [], "delivery_date": None, "evidence": [],
                         "missing_fields": ["x"], "ambiguities": ["y"], "suggested_change": None},
                        ensure_ascii=False)}]}

        adapter = OpenCodeAdapter("http://127.0.0.1:1", password="p")
        adapter._backend._transport = transport
        outcome = adapter.run_parse("ses-1", {"message": {}}, "job-1", 0.0,
                                    provider_id="opencode-go", model_id="qwen3.6-plus")
        self.assertEqual(seen[0]["model"], {"providerID": "opencode-go", "modelID": "qwen3.6-plus"})
        self.assertFalse(outcome.model_confirmed, "没回标识就不能说确认过")
        self.assertIn("未能独立确认", outcome.model_warning)

    def test_request_evidence_never_contains_credentials(self):
        """脱敏后的请求结构证据：不得包含认证头或密钥。"""
        adapter, seen = self._adapter({"intent": "unclear", "customer_alias": None, "target_order": None,
                                       "items": [], "delivery_date": None, "evidence": [],
                                       "missing_fields": ["x"], "ambiguities": ["y"],
                                       "suggested_change": None})
        outcome = adapter.run_parse("ses-1", {"message": {"content": "来两箱苹果"}}, "job-1", 0.0,
                                    provider_id="opencode-go", model_id="qwen3.6-plus")
        evidence = json.dumps(outcome.request_evidence, ensure_ascii=False)
        for forbidden in ("authorization", "Authorization", "api_key", "apiKey", "password", "Bearer "):
            self.assertNotIn(forbidden, evidence, f"请求证据里不得出现 {forbidden}")
        self.assertIn("model", json.dumps(outcome.request_evidence, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
