"""Tests for the local side of cloud parsing.

The cloud proposes; the merchant's own order store decides. These tests cover
the three properties that matter locally: prices and rules stay local, a
confirmed order is never silently rewritten, and a retried upload cannot create
a second order.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from pyweixin.client_login import MerchantClient, PendingQueue, Session
from pyweixin.gateway import ProductGateway
from pyweixin.local_parse import LOCAL_BLOCKED, LOCAL_DONE, LOCAL_NEEDS_RETRY, LocalParseService, describe_subscription
from pyweixin.opencode import LocalRuleAdapter
from pyweixin.orders import OrderParseResultInvalid, OrderStore, validate_order_parse_result
from pyweixin.product import ProductStore

from tests.test_product_gateway import VALID_PARSE_RESULT


def new_store():
    orders = OrderStore(":memory:")
    merchant = orders.create_merchant("本地店", plan="demo", ai_quota=0)
    tenant = merchant["merchant_id"]
    customer = orders.add_customer(tenant, "张老板", "老张", "群A")
    sku = orders.add_product(tenant, "SKU-1", "苹果", "箱", 5000, ["苹果"])
    return orders, tenant, customer, sku


def add_message(orders, tenant, group, content, customer_id=None, sent_at="2026-01-02T09:00:00+00:00", source="s1", sender="老张"):
    result = orders.add_import(
        tenant,
        group,
        [{"sender": sender, "content": content, "kind": "text", "sent_at": sent_at, "source_id": source}],
        source=source,
        source_type="test",
    )
    row = orders.db.execute("SELECT * FROM messages WHERE merchant_id=? ORDER BY rowid DESC LIMIT 1", (tenant,)).fetchone()
    if customer_id:
        orders.bind_message_customer(tenant, row["id"], customer_id, "tester")
        row = orders.db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
    del result
    return row


class SuggestionValidationTests(unittest.TestCase):
    def test_canonical_validator_accepts_a_well_formed_suggestion(self):
        validate_order_parse_result(VALID_PARSE_RESULT)

    def test_canonical_validator_rejects_every_malformed_shape(self):
        for broken in (
            "not a dict",
            {**VALID_PARSE_RESULT, "intent": "drop_database"},
            {**VALID_PARSE_RESULT, "items": "nope"},
            {k: v for k, v in VALID_PARSE_RESULT.items() if k != "evidence"},
            {**VALID_PARSE_RESULT, "items": [{"sku": "S", "quantity": True, "unit": "箱", "evidence": []}]},
            {**VALID_PARSE_RESULT, "customer_alias": 5},
            {**VALID_PARSE_RESULT, "suggested_change": []},
            {**VALID_PARSE_RESULT, "evidence": [1, 2]},
        ):
            with self.assertRaises(OrderParseResultInvalid):
                validate_order_parse_result(broken)

    def test_gateway_and_local_store_share_one_validator(self):
        """A shape rejected locally must also be rejected by the gateway."""
        from pyweixin.opencode import InvalidParseOutput, validate_parse_result

        broken = {**VALID_PARSE_RESULT, "intent": "not_an_intent"}
        with self.assertRaises(OrderParseResultInvalid):
            validate_order_parse_result(broken)
        with self.assertRaises(InvalidParseOutput):
            validate_parse_result(broken)


class ApplyCloudSuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orders, self.tenant, self.customer, self.sku = new_store()
        from pyweixin.dashboard import DashboardBuilder

        self.dashboard = DashboardBuilder(self.orders, self.tenant)

    def tearDown(self) -> None:
        self.orders.close()

    def test_suggestion_becomes_a_local_draft_priced_from_the_local_catalogue(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        # The cloud suggests a wildly wrong price; it must not be used at all.
        suggestion = {**VALID_PARSE_RESULT, "items": [{"sku": "SKU-1", "quantity": 2, "unit": "箱", "evidence": ["来两箱苹果"]}]}
        applied = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], suggestion, job_id="job_x")
        self.assertEqual(applied["outcome"], "drafts")
        detail = self.orders.order_detail(self.tenant, applied["order_id"])
        self.assertEqual(len(detail["items"]), 1)
        item = detail["items"][0]
        self.assertEqual(int(item["unit_price_cents"]), 5000, "price must come from the local catalogue")
        self.assertEqual(float(item["quantity"]), 2.0)
        total = sum(float(i["quantity"]) * int(i["unit_price_cents"]) for i in detail["items"]) / 100
        self.assertEqual(total, 100.0)

    def test_sku_outside_the_local_catalogue_becomes_a_clarification_not_an_order(self):
        """目录里没有的商品：**不建空订单**，开一条独立澄清任务。"""
        message = add_message(self.orders, self.tenant, "群A", "来两箱榴莲", self.customer)
        suggestion = {**VALID_PARSE_RESULT, "items": [{"sku": "SKU-UNKNOWN", "quantity": 2, "unit": "箱", "evidence": ["来两箱榴莲"]}]}
        applied = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], suggestion)
        self.assertEqual(applied["outcome"], "clarify")
        self.assertIsNone(applied["order_id"], "缺商品信息时不得建单")
        self.assertEqual(self.orders.list_orders(self.tenant), [], "澄清任务不是订单")
        self.assertIn("sku_outside_merchant_catalog", applied["ambiguities"])
        task = self.orders.clarification(self.tenant, applied["clarification_id"])
        self.assertFalse(task["counts_as_order"])
        self.assertTrue(task["suggested_question"], "澄清任务必须带一句要问的话")

    def test_evidence_not_present_in_the_source_is_flagged(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        suggestion = {**VALID_PARSE_RESULT, "evidence": ["客户其实说要十箱"]}
        applied = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], suggestion)
        self.assertEqual(applied["outcome"], "review")
        self.assertIn("evidence_not_in_source_message", applied["ambiguities"])

    def test_applying_the_same_suggestion_twice_does_not_double_order(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        first = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], VALID_PARSE_RESULT)
        second = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], VALID_PARSE_RESULT)
        self.assertEqual(first["outcome"], "drafts")
        self.assertEqual(second["outcome"], "duplicate")
        self.assertEqual(len(self.orders.list_orders(self.tenant)), 1)

    def test_a_confirmed_order_is_never_silently_rewritten(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        first = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], VALID_PARSE_RESULT)
        order_id = first["order_id"]
        self.orders.confirm(self.tenant, order_id, "tester")
        confirmed = self.orders.order_detail(self.tenant, order_id)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(float(confirmed["items"][0]["quantity"]), 2.0)

        # A later change must become a new reviewable version, not an edit.
        change = add_message(
            self.orders, self.tenant, "群A", "改成三箱", self.customer, sent_at="2026-01-03T09:00:00+00:00", source="s2"
        )
        suggestion = {
            **VALID_PARSE_RESULT,
            "intent": "modify",
            "target_order": order_id,
            "items": [{"sku": "SKU-1", "quantity": 3, "unit": "箱", "evidence": ["改成三箱"]}],
        }
        applied = self.orders.apply_cloud_suggestion(self.tenant, "群A", change["id"], suggestion)
        self.assertEqual(applied["outcome"], "drafts")
        self.assertNotEqual(applied["order_id"], order_id, "a change must not edit the confirmed order in place")
        after = self.orders.order_detail(self.tenant, order_id)
        self.assertEqual(float(after["items"][0]["quantity"]), 2.0, "the confirmed order must be untouched")
        self.assertEqual(after["status"], "confirmed")

    def test_unbound_customer_becomes_a_clarification_and_no_order(self):
        """身份不明确：开独立澄清任务，**不建空订单**。

        旧行为是建一张没有任何明细、也没有客户的空订单占位。它会进订单数、
        进待核对数，商户会以为有一笔生意要做，而实际上连客户是谁都还不知道。
        """
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", None, sender="没登记过的人")
        applied = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], VALID_PARSE_RESULT)
        self.assertEqual(applied["outcome"], "clarify")
        self.assertEqual(applied["reason"], "unidentified_customer")
        self.assertIsNone(applied["order_id"])
        self.assertEqual(self.orders.list_orders(self.tenant), [], "不得建空订单占位")
        tasks = self.orders.open_clarifications(self.tenant)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["reason"], "unidentified_customer")
        self.assertEqual(tasks[0]["message_id"], message["id"], "原始消息必须留痕")
        self.assertTrue(tasks[0]["parse_run_id"], "来源解析运行必须留痕")

    def test_message_from_another_merchant_is_invisible(self):
        other = self.orders.create_merchant("别的店", plan="demo", ai_quota=0)["merchant_id"]
        message = add_message(self.orders, other, "群B", "来两箱苹果", None)
        with self.assertRaises(ValueError):
            self.orders.apply_cloud_suggestion(self.tenant, "群B", message["id"], VALID_PARSE_RESULT)

    def test_invalid_suggestion_is_refused_before_anything_is_written(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        with self.assertRaises(OrderParseResultInvalid):
            self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], {"intent": "new_order"})
        self.assertEqual(self.orders.list_orders(self.tenant), [])

    def test_chitchat_intent_produces_no_order(self):
        message = add_message(self.orders, self.tenant, "群A", "今天天气不错", self.customer, source="chat-1")
        suggestion = {**VALID_PARSE_RESULT, "intent": "chitchat", "items": []}
        applied = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], suggestion)
        self.assertEqual(applied["outcome"], "ignored")
        self.assertEqual(self.orders.list_orders(self.tenant), [])

    def test_payment_claim_is_never_treated_as_a_receipt(self):
        """「钱转了」：只记录声称，不建单、不认定到账。

        没有可指向的订单时问客户是哪一单 —— 猜一张就等于把别人的单标成已付。
        """
        message = add_message(self.orders, self.tenant, "群A", "我已经转账了", self.customer, source="pay-1")
        suggestion = {**VALID_PARSE_RESULT, "intent": "payment_claim", "items": []}
        applied = self.orders.apply_cloud_suggestion(self.tenant, "群A", message["id"], suggestion)
        self.assertEqual(applied["outcome"], "clarify")
        self.assertIn("customer_payment_claim_not_receipt", applied["ambiguities"])
        self.assertIsNone(applied["order_id"], "付款声称不得新建订单")
        self.assertEqual(self.orders.list_orders(self.tenant), [])
        self.assertEqual(len(self.orders.open_clarifications(self.tenant)), 1)

    def test_payment_claim_is_attached_to_the_only_eligible_order(self):
        """能唯一定位到订单时：挂在那张单上，标成"声称已付款"。"""
        first = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer, source="p-base")
        created = self.orders.apply_cloud_suggestion(
            self.tenant, "群A", first["id"], {**VALID_PARSE_RESULT, "items": [{"sku": "SKU-1", "quantity": 2, "unit": "箱", "evidence": ["来两箱苹果"]}]}
        )
        self.orders.confirm(self.tenant, created["order_id"], "tester")
        claim = add_message(
            self.orders, self.tenant, "群A", "钱转了", self.customer, source="p-claim",
            sent_at="2026-01-05T09:00:00+00:00",
        )
        applied = self.orders.apply_cloud_suggestion(
            self.tenant, "群A", claim["id"],
            {**VALID_PARSE_RESULT, "intent": "payment_claim", "target_order": created["order_id"], "items": []},
        )
        self.assertEqual(applied["outcome"], "payment_claim")
        self.assertFalse(applied["acknowledged"], "到账必须人工核对，软件不得认定")
        self.assertEqual(applied["order_id"], created["order_id"])
        claims = self.dashboard.payment_claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["order_id"], created["order_id"])
        self.assertEqual(len(self.orders.list_orders(self.tenant)), 1, "不得新建第二张单")


def _loop_catalog():
    """本地解析回路测试用的模型目录。

    没有目录网关会如实拒绝任务（这是正确行为，不是缺陷），所以必须配置。
    """
    from pyweixin.model_catalog import (
        CatalogStatus,
        CatalogStore,
        OperatorModelPolicy,
        PricingClass,
        build_catalog,
    )

    policy = OperatorModelPolicy(
        alias="loop-default", provider_id="loop-provider", model_id="loop-model",
        pricing_class=PricingClass.METERED, is_default=True, summary="该模型会消耗分析额度",
    )
    catalog = CatalogStore()
    catalog.load_policies([policy.to_dict()])
    catalog.catalog = build_catalog(
        [{"provider_id": "loop-provider", "name": "回路供应商", "connected": True,
          "models": [{"model_id": "loop-model", "display_name": "回路模型", "context_limit": 1000,
                      "output_limit": 1000, "capabilities": {}, "upstream_cost": {},
                      "upstream_status": "live"}]}],
        {policy.alias: policy},
    )
    catalog.catalog.mark_status(policy.alias, CatalogStatus.SELECTABLE, verified=True, now=1.0)
    return catalog


class LocalParseLoopTests(unittest.TestCase):
    """The loop that sends work out and applies what comes back."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="pyweixin-loop-"))
        self.orders, self.tenant, self.customer, self.sku = new_store()
        self.store = ProductStore(":memory:")
        self.gateway = ProductGateway(
            store=self.store,
            adapter=LocalRuleAdapter(VALID_PARSE_RESULT),
            public_base_url="https://gateway.example",
            secure_cookies=False,
            catalog=_loop_catalog(),
        )
        from pyweixin.gateway_server import GatewayHTTPRequestHandler

        class Handler(GatewayHTTPRequestHandler):
            pass

        Handler.gateway = self.gateway
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.account = self.store.create_account("loop-shop", "password-1234", display_name="循环店", quota_micro_usd=20, max_concurrent_jobs=4)
        self.queue = PendingQueue(self.directory / "pending.db")
        self.session = self._login()

    def tearDown(self) -> None:
        self.gateway.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.orders.close()
        self.queue.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _login(self) -> Session:
        client = MerchantClient(self.base)
        real_start = client.start_login

        def start(device_name, platform="windows"):
            request = real_start(device_name, platform)
            self.store.approve_device_authorization(request["user_code"], self.account["account_id"])
            return request

        client.start_login = start  # type: ignore[method-assign]
        session = client.session
        client.session = session
        flow = self.store.start_device_authorization("循环电脑")
        self.store.approve_device_authorization(flow["user_code"], self.account["account_id"])
        polled = self.store.poll_device_authorization(flow["device_code"])
        return Session(
            access_token=polled["tokens"]["access_token"],
            refresh_token=polled["tokens"]["refresh_token"],
            device_id=polled["device"]["id"],
            merchant_name=polled["account"]["display_name"],
            device_name=polled["device"]["name"],
            subscription=self.store.get_subscription(self.account["account_id"]),
        )

    def _service(self) -> LocalParseService:
        # 真实客户端先读目录、记住计费策略版本，再提交。测试照做，否则会
        # 因为"没带版本"被 pricing_version_required 拒掉 —— 那是正确行为。
        catalog = self.gateway.catalog.for_client()
        return LocalParseService(
            orders=self.orders,
            client=MerchantClient(self.base, session=self.session),
            queue=self.queue,
            tenant=self.tenant,
            group="群A",
            model_alias=str(catalog["default_alias"]),
            pricing_version=str(catalog["version"]),
        )

    def _wait(self, service, job_id, timeout=15.0):
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            progress = service.settle()
            if progress.job_id == job_id and progress.state in {LOCAL_DONE, LOCAL_NEEDS_RETRY, LOCAL_BLOCKED}:
                return progress
            time.sleep(0.1)
        return service.settle()

    def test_full_loop_sends_out_and_applies_locally(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        service = self._service()
        events: list[tuple] = []
        progress = service.submit(message["id"], on_progress=lambda p: events.append((p.state, p.message)))

        self.gateway.start(workers=1)
        try:
            final = self._wait(service, progress.job_id)
        finally:
            self.gateway.stop()

        self.assertEqual(final.state, LOCAL_DONE)
        self.assertEqual(final.message, "已生成待确认订单")
        orders = self.orders.list_orders(self.tenant)
        self.assertEqual(len(orders), 1)
        detail = self.orders.order_detail(self.tenant, orders[0]["id"])
        self.assertEqual(int(detail["items"][0]["unit_price_cents"]), 5000)
        # The merchant interface saw the required plain-Chinese stages.
        messages = [message for _state, message in events]
        self.assertIn("解析排队中", messages)
        self.assertIn("解析处理中", messages)
        for _state, text in events:
            self.assertNotIn("token", text.lower())
            self.assertNotIn("session", text.lower())

    def test_outage_keeps_the_message_queued_and_local_work_continues(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        service = self._service()
        service.client.base_url = "http://127.0.0.1:1"  # nothing is listening
        progress = service.submit(message["id"])
        self.assertEqual(progress.state, LOCAL_NEEDS_RETRY)
        self.assertEqual(progress.message, "网络暂不可用")

        outstanding = self.queue.outstanding()
        self.assertEqual(len(outstanding), 1, "the message must be retained, not dropped")
        self.assertEqual(outstanding[0]["last_error"], "network_unavailable")

        # Local history, review and export keep working with no gateway at all.
        local = add_message(
            self.orders, self.tenant, "群A", "先记着两箱", self.customer, sent_at="2026-01-04T09:00:00+00:00", source="offline-1"
        )
        self.orders.apply_cloud_suggestion(
            self.tenant, "群A", local["id"], VALID_PARSE_RESULT
        )
        self.assertEqual(len(self.orders.list_orders(self.tenant)), 1)
        self.assertTrue(self.orders.dashboard(self.tenant) is not None)

    def test_expired_plan_blocks_new_work_with_a_plain_message(self):
        self.store.update_subscription(self.account["account_id"], status="suspended")
        self.session.subscription = self.store.get_subscription(self.account["account_id"])
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        service = self._service()
        progress = service.submit(message["id"])
        self.assertEqual(progress.state, LOCAL_BLOCKED)
        self.assertEqual(progress.message, "套餐已到期")
        # The message is retained for after renewal.
        self.assertEqual(len(self.queue.outstanding()), 1)

    def test_unbound_customer_is_blocked_before_anything_is_sent(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", None, sender="没登记过的人")
        service = self._service()
        progress = service.submit(message["id"])
        self.assertEqual(progress.state, LOCAL_BLOCKED)
        self.assertEqual(self.queue.outstanding(), [])

    def test_duplicate_submit_does_not_create_two_orders(self):
        message = add_message(self.orders, self.tenant, "群A", "来两箱苹果", self.customer)
        service = self._service()
        progress = service.submit(message["id"])
        self.gateway.start(workers=1)
        try:
            self._wait(service, progress.job_id)
            # A retried local submit reuses the same idempotency key.
            service.submit(message["id"])
            self._wait(service, progress.job_id)
        finally:
            self.gateway.stop()
        self.assertEqual(len(self.orders.list_orders(self.tenant)), 1)

    def test_request_carries_only_context_available_at_message_time(self):
        """A fact confirmed after the message must not leak into the request."""
        message = add_message(self.orders, self.tenant, "群A", "老样子", self.customer, sent_at="2026-01-01T09:00:00+00:00")
        with self.orders.db:
            self.orders.db.execute(
                "INSERT INTO memory_facts(id,merchant_id,customer_id,kind,value,source_order_id,confirmed,effective_from,effective_to,replaced_by)"
                " VALUES(?,?,?,?,?,NULL,1,?,NULL,NULL)",
                ("mf_future", self.tenant, self.customer, "preference", "只买进口", "2026-06-01T00:00:00+00:00"),
            )
        request = self._service().build_request(message["id"])
        self.assertEqual(request["history"]["customer_facts"], [], "future facts must not be sent")
        self.assertEqual(set(request["history"]), {"recent_messages", "customer_facts", "recent_orders"})
        self.assertTrue(all(item["sent_at"] <= message["sent_at"] for item in request["history"]["recent_messages"]))
        json.dumps(request, ensure_ascii=False)  # must be serialisable


class MerchantWordingTests(unittest.TestCase):
    def test_subscription_is_described_in_plain_chinese(self):
        active = describe_subscription({"active": True, "remaining_micro_usd": 42, "plan_code": "trial", "period_end": "2026-10-01T00:00:00+00:00"})
        self.assertIn("剩余额度：42", active["headline"])
        self.assertNotIn("token", active["headline"].lower())
        expired = describe_subscription({"active": False, "remaining_micro_usd": 0, "plan_code": "trial", "period_end": "2026-01-01T00:00:00+00:00"})
        self.assertIn("套餐已到期", expired["headline"])
        self.assertIn("本地订单", expired["detail"])
        self.assertEqual(describe_subscription({})["headline"], "未登录")

    def test_interface_text_contains_no_technical_terms(self):
        from pyweixin.merchant_ui import FORBIDDEN_TERMS, STATUS_LABELS, audit_text

        self.assertEqual(audit_text("剩余额度：42"), [])
        for label in STATUS_LABELS.values():
            self.assertEqual(audit_text(label), [], f"{label} leaks a technical term")
        for term in FORBIDDEN_TERMS:
            self.assertEqual(audit_text(term), [term])

    def test_every_required_merchant_state_has_a_label(self):
        """The interface must be able to show each state the product requires."""
        from pyweixin.client_login import (
            STATUS_CANCELLED,
            STATUS_LOGGED_IN,
            STATUS_LOGGED_OUT,
            STATUS_NETWORK_DOWN,
            STATUS_QUEUED,
            STATUS_RETRY,
            STATUS_RUNNING,
            STATUS_SUBSCRIPTION_EXPIRED,
            STATUS_WAITING_BROWSER,
        )
        from pyweixin.merchant_ui import merchant_labels

        expected = {
            STATUS_LOGGED_OUT,
            STATUS_WAITING_BROWSER,
            STATUS_LOGGED_IN,
            STATUS_QUEUED,
            STATUS_RUNNING,
            STATUS_RETRY,
            STATUS_NETWORK_DOWN,
            STATUS_SUBSCRIPTION_EXPIRED,
            STATUS_CANCELLED,
        }
        for label in expected:
            self.assertTrue(label and not label.isascii(), f"{label!r} is not plain Chinese text")
        self.assertEqual(merchant_labels("queued"), STATUS_QUEUED)
        self.assertEqual(merchant_labels("running"), STATUS_RUNNING)


class MerchantAppWiringTests(unittest.TestCase):
    """The desktop entry point must not need a network or a gateway to start."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="pyweixin-app-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_local_store_is_created_once_and_reused(self):
        from pyweixin.merchant_app import ensure_local_store

        path = self.directory / "orders.db"
        first_store, first_tenant = ensure_local_store(path)
        self.assertTrue(path.exists())
        first_store.close()
        second_store, second_tenant = ensure_local_store(path)
        second_store.close()
        self.assertEqual(first_tenant, second_tenant, "the merchant's local store must be reused, not recreated")

    def test_self_check_runs_with_no_gateway_configured(self):
        from pyweixin.merchant_app import main

        code = main(["--data-dir", str(self.directory), "--gateway", "http://127.0.0.1:9", "--check"])
        self.assertEqual(code, 0)
        self.assertTrue((self.directory / "orders.db").exists())
        self.assertTrue((self.directory / "pending.db").exists())
        # No credentials file is written before a real login.
        self.assertFalse((self.directory / "credentials.bin").exists())

    def test_credentials_are_not_written_by_a_failed_startup(self):
        from pyweixin.merchant_app import main

        main(["--data-dir", str(self.directory), "--check"])
        self.assertFalse((self.directory / "credentials.bin").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
