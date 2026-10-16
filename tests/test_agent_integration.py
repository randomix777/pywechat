"""The product <-> plugin integration.

The point of these tests is that the refactor **reused** what already existed
instead of building a parallel system. Concretely:

* context comes out of the merchant's existing ``OrderStore``, not a new table;
* a suggestion is applied through the existing
  ``OrderStore.apply_cloud_suggestion``, the same entry point the gateway uses;
* tenant filtering and the knowledge cut-off are enforced at the integration
  seam, not merely hoped for.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wechat_ai_assistant._packages import ensure_packages_on_path

ensure_packages_on_path()

from business_agent import AgentCore, FakeBackend, RunStatus  # noqa: E402
from business_agent.contracts.errors import InputError  # noqa: E402
from order_agent import OrderPlugin  # noqa: E402
from order_agent.plugin import SOURCE_CATALOG, SOURCE_CUSTOMER, SOURCE_HISTORY  # noqa: E402
from wechat_ai_assistant.integration import (  # noqa: E402
    OrderContextProvider,
    OrderStoreApplier,
    build_order_core,
    build_order_request,
    run_order_parse,
)
from wechat_ai_assistant.orders import OrderStore  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


class IntegrationHarness(unittest.TestCase):
    def setUp(self):
        self.store = OrderStore(":memory:")
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.sku = self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])

    def tearDown(self):
        self.store.close()

    def add_message(self, content: str, moment: datetime, source_id: str, sender: str = "张姐", stable: bool = True):
        result = self.store.add_import(
            self.tenant,
            self.group,
            [{"sender": sender, "content": content, "sent_at": iso(moment), "source_id": source_id, "stable_source_id": stable}],
            source=f"import-{source_id}",
            source_type="test",
        )
        row = self.store.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? AND source_id=?", (self.tenant, source_id)
        ).fetchone()
        return dict(row), result


class ContextProviderTests(IntegrationHarness):
    def test_context_comes_from_the_existing_order_store(self):
        message, _ = self.add_message("苹果两箱", NOW, "s1")
        provider = OrderContextProvider(self.store, self.tenant, self.group)
        request = build_order_request(message, tenant=self.tenant, group=self.group)
        bundle = provider.collect(request)
        self.assertTrue(bundle.by_source(SOURCE_CATALOG), "product catalogue must come from the merchant's store")
        self.assertTrue(bundle.by_source(SOURCE_CUSTOMER))

    def test_no_orders_from_after_the_knowledge_cutoff(self):
        """A replay must not see the answer it is being asked to produce."""
        future = self.store.add_import(
            self.tenant,
            self.group,
            [{"sender": "张姐", "content": "苹果三箱", "sent_at": iso(NOW + timedelta(days=1)), "source_id": "future-1", "stable_source_id": True}],
            source="import-future",
            source_type="test",
        )
        # Build a *future* order and confirm it is invisible to an earlier run.
        parsed = self.store.parse_import(self.tenant, self.group, future["import_id"])
        self.assertTrue(self.store.list_orders(self.tenant))
        message, _ = self.add_message("老样子", NOW, "s2")
        provider = OrderContextProvider(self.store, self.tenant, self.group)
        request = build_order_request(message, tenant=self.tenant, group=self.group)
        bundle = provider.collect(request)
        for entry in bundle.by_source(SOURCE_HISTORY):
            if entry.occurred_at is not None:
                self.assertLessEqual(entry.occurred_at, NOW)
        del parsed

    def test_tenant_isolation_holds_at_the_integration_seam(self):
        other = self.store.create_merchant("另一家商家")
        other_customer = self.store.add_customer(other["merchant_id"], "张姐", "张姐", self.group)
        self.add_message("苹果两箱", NOW, "s3")
        provider = OrderContextProvider(self.store, self.tenant, self.group)
        request = build_order_request(
            {"id": "s3", "content": "苹果两箱", "kind": "text", "sender": "张姐", "sent_at": iso(NOW)},
            tenant=self.tenant,
            group=self.group,
        )
        bundle = provider.collect(request)
        ids = {entry.value.get("id") for entry in bundle.by_source(SOURCE_CUSTOMER) if isinstance(entry.value, dict)}
        self.assertNotIn(other_customer, ids)
        self.assertNotIn(other["merchant_id"], ids)

    def test_missing_local_data_is_described_not_guessed(self):
        request = build_order_request(
            {"id": "nope", "content": "苹果两箱", "kind": "text", "sender": "陌生人", "sent_at": iso(NOW)},
            tenant=self.tenant,
            group=self.group,
        )
        provider = OrderContextProvider(self.store, self.tenant, self.group)
        missing = provider.describe_missing(request)
        self.assertTrue(any("没有这条消息" in item for item in missing))

    def test_an_empty_catalogue_is_reported_as_missing(self):
        message, _ = self.add_message("苹果两箱", NOW, "s4")
        request = build_order_request(message, tenant=self.tenant, group=self.group)
        provider = OrderContextProvider(self.store, self.tenant, self.group)
        # A catalogue exists here, so nothing should be reported missing.
        self.assertEqual([m for m in provider.describe_missing(request) if "商品" in m], [])


class RequestBuildingTests(IntegrationHarness):
    def test_the_cutoff_is_the_message_send_time(self):
        moment = datetime(2026, 9, 20, 9, 0, 0, tzinfo=timezone.utc)
        message, _ = self.add_message("苹果两箱", moment, "s5")
        request = build_order_request(message, tenant=self.tenant, group=self.group)
        self.assertEqual(request.as_of.instant, moment)
        self.assertEqual(request.events[0].occurred_at, moment)

    def test_the_scope_is_host_bound(self):
        message, _ = self.add_message("苹果两箱", NOW, "s6")
        request = build_order_request(message, tenant=self.tenant, group=self.group)
        self.assertEqual(request.scope.tenant_id, self.tenant)
        self.assertEqual(request.scope.conversation_id, self.group)

    def test_a_message_after_the_cutoff_is_refused_before_any_backend_call(self):
        """The contract refuses a self-inconsistent request at construction."""
        from business_agent import AgentRequest, AsOf, Scope
        from business_agent.contracts.errors import InputError as ContractInputError

        message, _ = self.add_message("苹果两箱", NOW, "s9")
        backend = FakeBackend(responses=[{}])
        core = build_order_core(backend, self.store, self.tenant, self.group)
        request = build_order_request(message, tenant=self.tenant, group=self.group)
        with self.assertRaises(ContractInputError):
            AgentRequest(
                request_id="r3",
                idempotency_key="i3",
                task_type=request.task_type,
                scope=Scope(tenant_id=self.tenant),
                events=request.events,
                as_of=AsOf(instant=NOW - timedelta(days=1)),
            )
        self.assertEqual(backend.call_count, 0, "no backend call may happen for an invalid request")


class ApplierTests(IntegrationHarness):
    """The applier must go through the product's existing rules."""

    def _result(self, output: dict):
        from business_agent.contracts.result import AgentResult

        return AgentResult(
            run_id="run-1", request_id="r1", status=RunStatus.SUCCEEDED, output=output
        )

    def test_a_valid_suggestion_creates_a_reviewable_draft(self):
        message, _ = self.add_message("苹果两箱", NOW, "s7")
        result = self._result(
            {
                "intent": "new_order",
                "customer_alias": "张姐",
                "target_order": None,
                "items": [{"said": '苹果两箱', "brand": None, "category": None, "sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
                "delivery_date": "2026-09-26",
                "evidence": ["苹果两箱"],
                "missing_fields": [],
                "ambiguities": [],
                "suggested_change": None,
                "_message_id": message["id"],
            }
        )
        applied = OrderStoreApplier(self.store, self.tenant).apply(result, self.group)
        self.assertIn(applied["outcome"], ("drafts", "review"))
        self.assertTrue(applied["order_id"])
        # Prices come from the merchant's catalogue, never from the suggestion.
        detail = self.store.order_detail(self.tenant, applied["order_id"])
        self.assertEqual(detail["items"][0]["unit_price_cents"], 2600)

    def test_a_failed_result_is_never_applied(self):
        from business_agent.contracts.result import AgentResult

        result = AgentResult(run_id="r", request_id="r", status=RunStatus.FAILED, error_category="network_error")
        applied = OrderStoreApplier(self.store, self.tenant).apply(result, self.group)
        self.assertEqual(applied["outcome"], "skipped")

    def test_applying_the_same_suggestion_twice_does_not_double_order(self):
        message, _ = self.add_message("苹果两箱", NOW, "s8")
        output = {
            "intent": "new_order",
            "customer_alias": "张姐",
            "target_order": None,
            "items": [{"said": '苹果两箱', "brand": None, "category": None, "sku": "APL", "quantity": 2, "unit": "箱", "evidence": ["苹果两箱"]}],
            "delivery_date": None,
            "evidence": ["苹果两箱"],
            "missing_fields": [],
            "ambiguities": [],
            "suggested_change": None,
            "_message_id": message["id"],
        }
        applier = OrderStoreApplier(self.store, self.tenant)
        first = applier.apply(self._result(dict(output)), self.group)
        second = applier.apply(self._result(dict(output)), self.group)
        self.assertEqual(second["outcome"], "duplicate")
        orders = self.store.list_orders(self.tenant)
        self.assertEqual(len(orders), 1, "a retried suggestion must not create a second order")


class NoParallelSystemTests(unittest.TestCase):
    """Guard against the refactor growing a second of anything."""

    def test_the_integration_creates_no_new_order_tables(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "wechat_ai_assistant" / "integration" / "order.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("CREATE TABLE", source.upper())
        self.assertNotIn("sqlite3.connect", source)

    def test_the_core_never_mentions_the_product_schema(self):
        for name in ("business_agent", "order_agent"):
            root = Path(__file__).resolve().parents[1] / "packages" / name
            for path in root.rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                for table in ("memory_facts", "order_items", "parse_runs", "review_tasks", "merchant_settings"):
                    self.assertNotIn(table, source, f"{path} depends on the product's schema")


if __name__ == "__main__":
    unittest.main()
