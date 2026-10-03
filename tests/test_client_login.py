"""Tests for the Windows merchant client: credential protection, device login
flow, offline queue and merchant-facing status wording."""
from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
import unittest
from pathlib import Path

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
    CredentialStore,
    DeviceLoginFlow,
    GatewayError,
    LoginError,
    MerchantClient,
    PendingQueue,
    Session,
    build_parse_request,
    build_verify_url,
    clear_session,
    restore_session,
    should_retry,
    status_label,
)
from pyweixin.gateway import ProductGateway
from pyweixin.opencode import LocalRuleAdapter
from pyweixin.product import ProductStore

from tests.test_product_gateway import VALID_PARSE_RESULT, sample_request

from http.server import ThreadingHTTPServer
import threading
from urllib.request import urlopen


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="pyweixin-client-"))
        self.path = self.directory / "credentials.bin"

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    @unittest.skipUnless(platform.system() == "Windows", "DPAPI requires Windows")
    def test_round_trip_through_dpapi_and_not_plaintext_on_disk(self):
        store = CredentialStore(self.path)
        store.save({"session": {"access_token": "secret-token-value", "merchant_name": "一号店"}})
        raw = self.path.read_bytes()
        self.assertNotIn(b"secret-token-value", raw, "credentials must not be readable on disk")
        self.assertNotIn("一号店".encode("utf-8"), raw)
        self.assertTrue(store.load()["session"]["access_token"] == "secret-token-value")
        store.clear()
        self.assertIsNone(store.load())

    @unittest.skipUnless(platform.system() == "Windows", "DPAPI requires Windows")
    def test_stored_blob_is_opaque_and_not_reusable_json(self):
        store = CredentialStore(self.path)
        store.save({"session": {"refresh_token": "refresh-secret"}})
        raw = self.path.read_bytes()
        self.assertNotIn(b"refresh-secret", raw)
        # The blob is not a readable JSON document either, so copying it to
        # another account or machine yields nothing usable.
        with self.assertRaises(ValueError):
            json.loads(raw.decode("utf-8", errors="strict"))

    def test_missing_file_returns_none(self):
        self.assertIsNone(CredentialStore(self.path).load())

    @unittest.skipIf(platform.system() == "Windows", "non-Windows guard")
    def test_refuses_to_store_on_non_windows(self):
        with self.assertRaises(LoginError):
            CredentialStore(self.path).save({"session": {}})
        self.assertFalse(self.path.exists(), "no plaintext fallback may be written")

    def test_plain_text_file_is_never_created(self):
        store = CredentialStore(self.directory / "nested" / "creds.dat")
        try:
            store.save({"session": {"x": 1}})
        except LoginError:
            pass  # non-Windows: refusing to persist is the correct outcome
        self.assertEqual(list(self.directory.rglob("*.json")), [], "no plaintext config may be written")
        if self.path_for(store).exists():
            self.assertNotIn(b'"x"', self.path_for(store).read_bytes())

    @staticmethod
    def path_for(store: CredentialStore) -> Path:
        return store.path


class StatusWordingTests(unittest.TestCase):
    def test_merchant_never_sees_technical_terms(self):
        labels = [
            STATUS_LOGGED_OUT,
            STATUS_WAITING_BROWSER,
            STATUS_LOGGED_IN,
            STATUS_NETWORK_DOWN,
            STATUS_SUBSCRIPTION_EXPIRED,
            STATUS_QUEUED,
            STATUS_RUNNING,
            STATUS_RETRY,
        ]
        forbidden = ("token", "api key", "apikey", "provider", "session", "bearer", "opencode", "模型地址")
        for label in labels:
            lowered = label.lower()
            for term in forbidden:
                self.assertNotIn(term, lowered, f"{label!r} exposes {term!r}")

    def test_state_and_error_map_to_plain_chinese(self):
        self.assertEqual(status_label("queued"), STATUS_QUEUED)
        self.assertEqual(status_label("running"), STATUS_RUNNING)
        self.assertEqual(status_label("outcome_unknown"), STATUS_RETRY)
        self.assertEqual(status_label("cancelled"), STATUS_CANCELLED)
        self.assertEqual(status_label("queued", "network_unavailable"), STATUS_NETWORK_DOWN)
        self.assertEqual(status_label("queued", "subscription_inactive"), STATUS_SUBSCRIPTION_EXPIRED)
        self.assertEqual(status_label("something-new"), STATUS_RETRY)

    def test_only_transient_failures_are_retried(self):
        # A network outage or an unconfirmed result is worth retrying.
        self.assertTrue(should_retry("network_unavailable", attempts=1))
        self.assertTrue(should_retry("backend_unavailable", attempts=1))
        self.assertTrue(should_retry("outcome_unknown", attempts=1))
        # Billing and validation problems are not fixed by trying again.
        self.assertFalse(should_retry("quota_exhausted", attempts=1))
        self.assertFalse(should_retry("subscription_inactive", attempts=1))
        self.assertFalse(should_retry("unsupported_field", attempts=1))
        # Give up eventually so a stuck message cannot retry forever.
        self.assertFalse(should_retry("network_unavailable", attempts=99))


class PendingQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="pyweixin-queue-")
        self.queue = PendingQueue(os.path.join(self.directory, "pending.db"))

    def tearDown(self) -> None:
        self.queue.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_messages_survive_an_outage_and_keep_their_content(self):
        key = self.queue.enqueue({"message": {"content": "来两箱苹果"}})
        outstanding = self.queue.outstanding()
        self.assertEqual(len(outstanding), 1)
        self.assertEqual(outstanding[0]["idempotency_key"], key)
        ready = self.queue.ready()
        self.assertEqual(ready[0]["request"]["message"]["content"], "来两箱苹果")

    def test_enqueue_is_idempotent_per_key(self):
        key = self.queue.enqueue({"message": {"content": "a"}}, idempotency_key="fixed-1")
        again = self.queue.enqueue({"message": {"content": "a"}}, idempotency_key="fixed-1")
        self.assertEqual(key, again)
        self.assertEqual(len(self.queue.outstanding()), 1)

    def test_retry_is_backed_off_then_becomes_ready(self):
        clock = [1000.0]
        queue = PendingQueue(":memory:", clock=lambda: clock[0])
        queue.enqueue({"message": {"content": "b"}})
        entry = queue.ready()[0]
        queue.mark_retry(entry["id"], "network_unavailable", delay=60)
        self.assertEqual(queue.ready(), [])
        self.assertEqual(queue.outstanding()[0]["last_error"], "network_unavailable")
        clock[0] += 61
        self.assertEqual(len(queue.ready()), 1)
        self.assertEqual(queue.outstanding()[0]["attempts"], 1)
        queue.close()

    def test_submitted_entries_leave_the_retry_queue(self):
        entry = self.queue.enqueue({"message": {"content": "c"}}) and self.queue.ready()[0]
        self.queue.mark_submitted(entry["id"], "job_123")
        self.assertEqual(self.queue.outstanding(), [])
        self.assertEqual(self.queue.ready(), [])


def _client_catalog() -> Any:
    """客户端测试用的模型目录。

    没有目录网关会如实拒绝任务（这是正确行为），所以这里显式配置。
    """
    from pyweixin.model_catalog import (
        CatalogStatus,
        CatalogStore,
        OperatorModelPolicy,
        PricingClass,
        build_catalog,
    )

    policy = OperatorModelPolicy(
        alias="client-free", provider_id="test-provider", model_id="test-model",
        pricing_class=PricingClass.METERED, is_default=True, summary="该模型会消耗分析额度",
    )
    catalog = CatalogStore()
    catalog.load_policies([policy.to_dict()])
    catalog.catalog = build_catalog(
        [{"provider_id": "test-provider", "name": "测试供应商", "connected": True,
          "models": [{"model_id": "test-model", "display_name": "测试模型", "context_limit": 1000,
                      "output_limit": 1000, "capabilities": {}, "upstream_cost": {},
                      "upstream_status": "live"}]}],
        {policy.alias: policy},
    )
    catalog.catalog.mark_status(policy.alias, CatalogStatus.SELECTABLE, verified=True, now=1.0)
    return catalog


class ClientAgainstRealGatewayTests(unittest.TestCase):
    """Exercises the client against the real gateway HTTP surface."""

    def setUp(self) -> None:
        self.store = ProductStore(":memory:")
        self.gateway = ProductGateway(
            store=self.store,
            adapter=LocalRuleAdapter(VALID_PARSE_RESULT),
            public_base_url="https://gateway.example",
            secure_cookies=False,
            catalog=_client_catalog(),
        )
        from pyweixin.gateway_server import GatewayHTTPRequestHandler

        class Handler(GatewayHTTPRequestHandler):
            pass

        Handler.gateway = self.gateway
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.account = self.store.create_account("client-shop", "password-1234", display_name="客户端店", quota_units=9)
        self.directory = Path(tempfile.mkdtemp(prefix="pyweixin-login-"))
        self.credentials = CredentialStore(self.directory / "creds.bin")

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _approve_in_browser(self, user_code: str) -> None:
        self.store.approve_device_authorization(user_code, self.account["account_id"])

    @staticmethod
    def _virtual_clock():
        """A clock that only advances when the flow 'sleeps', so tests are fast
        and cannot busy-loop against wall time."""
        state = {"now": 0.0}

        def clock() -> float:
            return state["now"]

        def sleep(seconds: float) -> None:
            state["now"] += max(0.0, float(seconds))

        return clock, sleep

    def test_login_flow_end_to_end_with_credential_persistence(self):
        client = MerchantClient(self.base)
        progress: list[str] = []
        real_start = client.start_login
        approved: list[str] = []

        def start_and_confirm(device_name, platform="windows"):
            # Stand in for the merchant confirming in the browser.
            request = real_start(device_name, platform)
            self._approve_in_browser(request["user_code"])
            approved.append(request["user_code"])
            return request

        clock, sleep = self._virtual_clock()
        client.start_login = start_and_confirm  # type: ignore[method-assign]
        device_flow = DeviceLoginFlow(
            client,
            self.credentials,
            "收银台电脑",
            open_browser=False,
            on_progress=lambda p: progress.append(p.status),
            clock=clock,
            sleep=sleep,
        )
        session = device_flow.run()
        self.assertEqual(len(approved), 1)
        self.assertEqual(session.merchant_name, "客户端店")
        self.assertEqual(session.device_name, "收银台电脑")
        self.assertIn(STATUS_WAITING_BROWSER, progress)
        self.assertIn(STATUS_LOGGED_IN, progress)
        self.assertEqual(session.remaining_units, 9)

        client.session = session
        self.assertEqual(client.account()["merchant"]["display_name"], "客户端店")

        # Credentials survive a client restart.
        if platform.system() == "Windows":
            restored = restore_session(self.credentials)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.refresh_token, session.refresh_token)
            clear_session(self.credentials)
            self.assertIsNone(restore_session(self.credentials))

    def test_login_reports_denial_in_plain_chinese(self):
        client = MerchantClient(self.base)
        clock, sleep = self._virtual_clock()
        real_start = client.start_login

        def start(device_name, platform="windows"):
            request = real_start(device_name, platform)
            self.store.deny_device_authorization(request["user_code"])
            return request

        client.start_login = start  # type: ignore[method-assign]
        flow = DeviceLoginFlow(
            client, self.credentials, "会被拒绝的电脑", open_browser=False, clock=clock, sleep=sleep
        )
        with self.assertRaises(LoginError) as ctx:
            flow.run()
        self.assertIn("取消", str(ctx.exception))

    def test_login_reports_expiry_in_plain_chinese(self):
        client = MerchantClient(self.base)
        clock, sleep = self._virtual_clock()
        flow = DeviceLoginFlow(
            client, self.credentials, "会过期的电脑", open_browser=False, clock=clock, sleep=sleep
        )
        with self.assertRaises(LoginError) as ctx:
            flow.run()
        self.assertIn("过期", str(ctx.exception))

    def test_login_reports_network_outage_and_keeps_waiting(self):
        client = MerchantClient(self.base)
        seen: list[str] = []
        attempts = {"n": 0}
        real_start = client.start_login
        request_box: dict = {}

        def flaky_poll(code: str):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise GatewayError("network_unavailable", STATUS_NETWORK_DOWN)
            if attempts["n"] == 2:
                self._approve_in_browser(request_box["user_code"])
            return self.store.poll_device_authorization(code)

        def start(device_name, platform="windows"):
            request = real_start(device_name, platform)
            request_box["user_code"] = request["user_code"]
            return request

        clock, sleep = self._virtual_clock()
        client.start_login = start  # type: ignore[method-assign]
        client.poll_login = flaky_poll  # type: ignore[method-assign]
        flow = DeviceLoginFlow(
            client,
            self.credentials,
            "断网电脑",
            open_browser=False,
            on_progress=lambda p: seen.append(p.status),
            clock=clock,
            sleep=sleep,
        )
        session = flow.run()
        self.assertIn(STATUS_NETWORK_DOWN, seen, "a transient outage must be shown, not raised")
        self.assertEqual(session.merchant_name, "客户端店")

    def test_client_submits_a_job_with_a_stable_idempotency_key(self):
        client = MerchantClient(self.base)
        clock, sleep = self._virtual_clock()
        real_start = client.start_login

        def start(device_name, platform="windows"):
            request = real_start(device_name, platform)
            self._approve_in_browser(request["user_code"])
            return request

        client.start_login = start  # type: ignore[method-assign]
        session = DeviceLoginFlow(
            client, self.credentials, "下单电脑", open_browser=False, clock=clock, sleep=sleep
        ).run()
        client.session = session

        body = build_parse_request(
            {"id": "m1", "sender": "张老板", "content": "来两箱苹果", "sent_at": "2026-01-01T10:00:00+08:00"},
            {"id": "c1", "name": "张老板", "alias": "老张"},
            [{"sku": "SKU-1", "name": "苹果", "unit": "箱", "aliases": "苹果"}],
        )
        # 真实客户端先读目录记住版本，提交时带上。
        body["pricing_version"] = self.gateway.catalog.for_client()["version"]
        first = client.submit_parse(body, "msg-m1")
        second = client.submit_parse(body, "msg-m1")
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
        self.assertEqual(client.job(first["job"]["job_id"])["job"]["state"], "queued")
        cancelled = client.cancel(first["job"]["job_id"])
        self.assertEqual(cancelled["job"]["state"], "cancelled")

    def test_expired_subscription_is_surfaced_as_plain_chinese(self):
        client = MerchantClient(self.base)
        clock, sleep = self._virtual_clock()
        real_start = client.start_login

        def start(device_name, platform="windows"):
            request = real_start(device_name, platform)
            self._approve_in_browser(request["user_code"])
            return request

        client.start_login = start  # type: ignore[method-assign]
        session = DeviceLoginFlow(
            client, self.credentials, "过期电脑", open_browser=False, clock=clock, sleep=sleep
        ).run()
        client.session = session
        self.store.update_subscription(self.account["account_id"], status="suspended")
        payload = sample_request()
        payload["pricing_version"] = self.gateway.catalog.for_client()["version"]
        with self.assertRaises(GatewayError) as ctx:
            client.submit_parse(payload, "k-expired")
        self.assertEqual(ctx.exception.code, "subscription_inactive")
        self.assertEqual(status_label("queued", ctx.exception.code), STATUS_SUBSCRIPTION_EXPIRED)

    def test_gateway_outage_raises_a_network_label_not_a_crash(self):
        client = MerchantClient(self.base, session=Session(access_token="t", refresh_token="r"))
        self.httpd.shutdown()
        with self.assertRaises(GatewayError) as ctx:
            client.account()
        self.assertEqual(ctx.exception.code, "network_unavailable")
        self.assertEqual(ctx.exception.merchant_message, STATUS_NETWORK_DOWN)

    def test_remote_plaintext_gateway_is_refused(self):
        with self.assertRaises(ValueError):
            MerchantClient("http://gateway.example.com")
        MerchantClient("http://127.0.0.1:8080")  # loopback is allowed for local testing


class ParseRequestMinimisationTests(unittest.TestCase):
    def test_request_carries_only_the_selected_context(self):
        request = build_parse_request(
            {"id": "m1", "sender": "张老板", "content": "老样子", "sent_at": "2026-01-01T10:00:00+08:00"},
            {"id": "c1", "name": "张老板", "alias": "老张", "address": "某地"},
            [{"sku": "SKU-1", "name": "苹果", "unit": "箱"}],
            recent_messages=[{"sender": "张老板", "content": "昨天两箱", "sent_at": "2025-12-31T10:00:00+08:00"}],
            customer_facts=[{"kind": "preference", "value": "要红富士"}],
            recent_orders=[{"order_id": "o1", "status": "confirmed"}],
            order_state={"status": "draft"},
        )
        self.assertEqual(set(request), {"message", "customer", "products", "history", "order_state"})
        self.assertNotIn("system_prompt", json.dumps(request))
        self.assertNotIn("api_key", json.dumps(request).lower())
        # The merchant's full local history is never serialised wholesale.
        self.assertEqual(len(request["history"]["recent_messages"]), 1)

    def test_verify_url_is_built_without_the_device_code(self):
        url = build_verify_url("https://gateway.example", "BCDF-2345")
        self.assertIn("code=BCDF-2345", url)
        self.assertNotIn("device_code", url)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
