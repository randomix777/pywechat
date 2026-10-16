"""Production JYI credentials must never fall into the old product protocol."""
from __future__ import annotations

import sys
import io
import json
import unittest
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_ai_assistant.client_login import GatewayError, MerchantClient, Session, restore_session
from wechat_ai_assistant.jyi_client import (
    BUSINESS_NOTICE, JYI_AUTH_ORIGIN, JYI_GATEWAY, JYIClient,
    create_merchant_client, production_oauth_config,
)
from wechat_ai_assistant.oauth_browser import BrowserLoginFlow, OAuthError


class JYILoginTests(unittest.TestCase):
    def response(self, **changes):
        return {"ok": True, "key_status": "active", "base_url": JYI_GATEWAY,
                "api_key": "jyi_sk_test_only", "device_token": "test-device-token",
                "connection_id": "cnx_test", "allocated_micros": 1234,
                "balance_known": True, "spendable_credits": 1200000,
                "quota_micro_usd": 200, "used_micro_usd": 50,
                "reserved_micro_usd": 10, "remaining_micro_usd": 140,
                "account_email": "merchant@example.com", **changes}

    def test_browser_callback_exchange_and_restore(self):
        client = JYIClient()
        store = Mock()
        progress = Mock()
        requests = []

        def authorize(url):
            parsed = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(parsed.hostname, "ai.jyisolutions.com")
            self.assertEqual(parsed.path, "/connect/authorize")
            self.assertEqual(query["client_id"], ["wechat_ai_assistant-desktop"])
            self.assertEqual(query["code_challenge_method"], ["S256"])
            requests.append(query)
            callback = query["redirect_uri"][0] + "?" + urllib.parse.urlencode({
                "code": "single-use-code", "state": query["state"][0],
            })
            with urllib.request.urlopen(callback, timeout=3) as response:
                self.assertEqual(response.status, 200)
            return True

        with patch("wechat_ai_assistant.jyi_client.BrowserLoginFlow",
                   side_effect=lambda **kwargs: BrowserLoginFlow(**kwargs, open_browser=authorize, timeout=3)), \
                patch.object(client.auth_client, "_request", return_value=self.response()) as exchange:
            session = client.login(store, "电脑", progress)
        method, path, body = exchange.call_args.args
        self.assertEqual((method, path), ("POST", "/oauth/token"))
        self.assertEqual(body["redirect_uri"], requests[0]["redirect_uri"][0])
        self.assertEqual(body["grant_type"], "authorization_code")
        self.assertTrue(body["code_verifier"])
        self.assertFalse(exchange.call_args.kwargs["authenticated"])
        self.assertEqual(session.access_token, "jyi_sk_test_only")
        self.assertEqual(session.subscription["remaining_micro_usd"], 140)
        self.assertEqual(session.merchant_name, "merchant@example.com")
        self.assertEqual(session.expires_at, 0)
        store.load.return_value = store.save.call_args.args[0]
        restored = restore_session(store)
        self.assertEqual(restored.provider, "jyi")
        self.assertIsNotNone(JYIClient(session=restored).session)

    def test_production_dispatch_and_old_credentials_are_isolated(self):
        old = Session("product-token", "product-refresh")
        self.assertIsInstance(create_merchant_client(JYI_GATEWAY + "/v1", old), JYIClient)
        self.assertIsNone(create_merchant_client(JYI_GATEWAY, old).session)
        self.assertIsInstance(create_merchant_client("http://127.0.0.1:8080", old), MerchantClient)
        jyi = Session("jyi_sk_test", "device", provider="jyi", gateway_url=JYI_GATEWAY,
                      auth_origin=JYI_AUTH_ORIGIN)
        self.assertIsNone(create_merchant_client("http://127.0.0.1:8080", jyi).session)
        jyi.auth_origin = "https://different.example.com"
        self.assertIsNone(JYIClient(session=jyi).session)

    def test_wrong_gateway_and_suspended_account_are_not_saved(self):
        for values in ({"base_url": "https://other.example.com"},
                       {"key_status": "account_suspended"}, {"api_key": "sk-or-legacy"}):
            client, store = JYIClient(), Mock()
            def run(flow):
                return flow.exchange(code="code", redirect_uri="http://127.0.0.1:1234/oauth/callback",
                                     code_verifier="verifier")
            with patch.object(BrowserLoginFlow, "run", run), \
                    patch.object(client.auth_client, "_request", return_value=self.response(**values)):
                with self.assertRaises(OAuthError):
                    client.login(store, "电脑", Mock())
            store.save.assert_not_called()

    def test_device_status_refreshes_balance_without_rotating_key(self):
        session = Session("jyi_sk_test", "device", "cnx_test", provider="jyi",
                          gateway_url=JYI_GATEWAY, auth_origin=JYI_AUTH_ORIGIN)
        client = JYIClient(session=session)
        with patch.object(client.auth_client, "_request", return_value=self.response(api_key=None)) as request:
            account = client.account()
            client.refresh()
        self.assertEqual(request.call_args.args[1], "/oauth/credential")
        self.assertEqual(request.call_args.args[2], {"connection_id": "cnx_test", "device_token": "device"})
        self.assertEqual(account["subscription"]["remaining_micro_usd"], 140)
        self.assertEqual(account["message"], "")
        self.assertEqual(client.session.access_token, "jyi_sk_test")

    def test_business_requests_are_not_sent_to_missing_endpoints(self):
        client = JYIClient()
        with patch.object(client, "_request") as request:
            for submit in (client.submit_parse, client.submit_summary):
                with self.assertRaises(GatewayError) as error:
                    submit({}, "test-request")
                self.assertEqual(error.exception.code, "business_api_unavailable")
            client.logout()
        request.assert_not_called()

    def test_jyi_platform_and_model_gateway_error_shapes(self):
        client = JYIClient()
        for body, code in (({"ok": False, "code": "unauthorized", "message": "Revoked"}, "unauthorized"),
                           ({"error": {"code": "invalid_api_key", "message": "Invalid key"}}, "invalid_api_key")):
            error = urllib.error.HTTPError(JYI_GATEWAY, 401, "Unauthorized", {},
                                           io.BytesIO(json.dumps(body).encode()))
            with patch.object(client._opener, "open", side_effect=error):
                with self.assertRaises(GatewayError) as raised:
                    client.list_models()
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.status, 401)

    def test_quota_fields_map_to_subscription(self):
        """映射服务端的实际余额快照字段。"""
        session = Session("jyi_sk_test", "device", "cnx_test", provider="jyi",
                          gateway_url=JYI_GATEWAY, auth_origin=JYI_AUTH_ORIGIN)
        client = JYIClient(session=session)
        credential = self.response(quota_micro_usd=200, used_micro_usd=50,
                                   reserved_micro_usd=10, remaining_micro_usd=140,
                                   plan_code="pro", period_end="2026-11-12T00:00:00")
        with patch.object(client.auth_client, "_request", return_value=credential):
            account = client.account()
        subscription = account["subscription"]
        self.assertEqual(subscription["quota_micro_usd"], 200)
        self.assertEqual(subscription["remaining_micro_usd"], 140)
        self.assertEqual(subscription["plan_code"], "pro")

    def test_unknown_balance_stays_unknown_and_network_recovery_refreshes_it(self):
        from wechat_ai_assistant.wizard import QuotaView, Wizard

        class Client:
            session = Session("token", "device")
            calls = 0

            def account(self):
                self.calls += 1
                if self.calls == 1:
                    raise GatewayError("network_unavailable", "offline")
                return {"subscription": {
                    "balance_known": True, "active": True, "quota_micro_usd": 100,
                    "used_micro_usd": 30, "reserved_micro_usd": 0,
                    "remaining_micro_usd": 70,
                }}

        wizard = Wizard.__new__(Wizard)
        wizard.client = Client()
        wizard.quota = QuotaView(logged_in=True, balance_known=False)
        first = wizard.check_quota()
        self.assertTrue(first.logged_in)
        self.assertFalse(first.balance_known)
        self.assertIn("联网后自动刷新", first.headline())
        second = wizard.check_quota()
        self.assertTrue(second.balance_known)
        self.assertEqual(second.remaining, 70)


if __name__ == "__main__":
    unittest.main()
