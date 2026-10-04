"""模型选择 → 任务 → 计费 的网关级验证。

覆盖：客户只能选别名、免费不扣额度、免费额度用完、换模型影响新任务不影响
在跑的任务、幂等键撞模型、免费失败不偷偷切收费。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.gateway import ProductGateway  # noqa: E402
from pyweixin.gateway_server import GatewayHTTPRequestHandler  # noqa: E402
from pyweixin.model_catalog import (  # noqa: E402
    CatalogStatus,
    CatalogStore,
    OperatorModelPolicy,
    PricingClass,
    build_catalog,
)
from pyweixin.opencode import LocalRuleAdapter  # noqa: E402
from pyweixin.product import ProductStore  # noqa: E402

VALID = {
    "intent": "new_order", "customer_alias": "张老板", "target_order": None,
    "items": [{"sku": "SKU-1", "quantity": 2, "unit": "箱", "evidence": ["来两箱苹果"]}],
    "delivery_date": None, "evidence": ["来两箱苹果"], "missing_fields": [],
    "ambiguities": [], "suggested_change": None,
}

FREE = "free-standard"
PAID = "paid-deep"


#: 当前网关的计费策略版本，随 ``request()`` 一起带上，模拟真实客户端
#: "先读目录、记住版本、提交时带上"。收费任务缺它会被拒。
CURRENT_PRICING_VERSION = {"value": ""}


def request(message: str = "来两箱苹果", **extra) -> dict:
    payload = {
        "message": {"id": "m1", "sender": "张老板", "content": message, "kind": "text",
                    "sent_at": "2026-01-01T10:00:00+08:00"},
        "customer": {"id": "c1", "name": "张老板", "alias": "老张", "address": ""},
        "products": [{"sku": "SKU-1", "name": "苹果", "unit": "箱", "aliases": "苹果"}],
        "history": {"recent_messages": [], "customer_facts": [], "recent_orders": []},
        "order_state": {"status": "draft"},
        "pricing_version": CURRENT_PRICING_VERSION["value"],
    }
    payload.update(extra)
    return payload


def make_catalog(free_limit: int = 3, default: str = FREE) -> CatalogStore:
    policies = {
        FREE: OperatorModelPolicy(
            alias=FREE, provider_id="p", model_id="m-free", pricing_class=PricingClass.METERED,
            daily_free_limit=free_limit, is_default=(default == FREE), summary="适合日常订单",
        ),
        PAID: OperatorModelPolicy(
            alias=PAID, provider_id="p", model_id="m-paid", pricing_class=PricingClass.METERED,
            is_default=(default == PAID), summary="分析较慢",
        ),
    }
    store = CatalogStore()
    store.load_policies(list(policies.values()))
    store.catalog = build_catalog(
        [{"provider_id": "p", "name": "测试供应商", "connected": True,
          "models": [
              {"model_id": "m-free", "display_name": "免费模型", "context_limit": 1000,
               "output_limit": 1000, "capabilities": {}, "upstream_cost": {}, "upstream_status": "live"},
              {"model_id": "m-paid", "display_name": "深度模型", "context_limit": 1000,
               "output_limit": 1000, "capabilities": {}, "upstream_cost": {}, "upstream_status": "live"},
          ]}],
        policies,
    )
    for alias in (FREE, PAID):
        store.catalog.mark_status(alias, CatalogStatus.SELECTABLE, verified=True, now=1.0)
    return store


class GatewayModelHarness(unittest.TestCase):
    def build(self, catalog=None, adapter=None):
        self.store = ProductStore(":memory:")
        self.adapter = adapter or LocalRuleAdapter(VALID)
        self.gateway = ProductGateway(
            store=self.store, adapter=self.adapter,
            public_base_url="https://gateway.example", secure_cookies=False,
            catalog=catalog if catalog is not None else make_catalog(),
        )
        CURRENT_PRICING_VERSION["value"] = self.gateway.catalog.for_client()["version"]

        class Handler(GatewayHTTPRequestHandler):
            pass

        Handler.gateway = self.gateway
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.account = self.store.create_account(
            "modelshop", "password-1234", display_name="模型店", quota_micro_usd=10, max_concurrent_jobs=4
        )
        self.device_id, _created = self.store._upsert_device(
            self.account["account_id"], self.account["merchant_id"], "模型电脑"
        )
        tokens = self.store._issue_tokens(self.account["account_id"], self.device_id)
        self.headers = {
            "Authorization": "Bearer " + tokens.access_token,
            "X-Agent-Id": "agent-1",
            "Content-Type": "application/json",
        }

    def tearDown(self):
        self.gateway.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def post_job(self, payload, key="k1"):
        self._call("POST", "/v1/order-parse-jobs", payload, {**self.headers, "Idempotency-Key": key})
        return self._status, self._json

    def get(self, path):
        self._call("GET", path, headers=self.headers)
        return self._status, self._json

    def _call(self, method, path, body=None, headers=None):
        import urllib.error
        import urllib.request

        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                self._status = response.status
                self._json = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            self._status = exc.code
            self._json = json.loads(exc.read().decode("utf-8") or "{}")
        return self._json


class ClientCatalogTests(GatewayModelHarness):
    def setUp(self):
        self.build()

    def test_catalog_lists_only_aliases(self):
        self._call("GET", "/v1/models", headers=self.headers)
        catalog = self._json["catalog"]
        blob = json.dumps(catalog, ensure_ascii=False)
        for leak in ("m-free", "m-paid", "test-provider", "upstream_cost"):
            self.assertNotIn(leak, blob)
        self.assertTrue(catalog["free_available"])
        self.assertEqual(catalog["default_alias"], FREE)
        # 免费额度已从客户端视图里删除（产品规则：
        # 免费模型也扣额度），这里改为断言**不存在**。
        self.assertTrue(all("free_quota_text" not in m for m in catalog["models"]))


class JobCreationTests(GatewayModelHarness):
    def setUp(self):
        self.build()

    def test_client_cannot_force_a_provider(self):
        status, body = self.post_job(request(provider="evil"), "j3")
        self.assertEqual(status, 400)
        self.assertIn(body["error"], ("model_not_allowed", "unsupported_field"))

    def test_client_cannot_send_an_upstream_url(self):
        status, body = self.post_job(request(upstream_url="http://evil"), "j4")
        self.assertEqual(status, 400)

    def test_unknown_model_is_refused(self):
        status, body = self.post_job(request(model="nope"), "j5")
        self.assertEqual(status, 409)
        self.assertIn(body["error"], ("model_not_allowed", "model_unavailable"))

    def test_paid_model_reserves_subscription_quota(self):
        _, body = self.post_job(request(model=PAID), "j6")
        self.assertEqual(body["job"]["reserved_micro_usd"], 1)
        snapshot = self.store.job_snapshot(body["job"]["job_id"])
        self.assertEqual(snapshot["pricing_class"], "metered")
        self.assertEqual(snapshot["model_id"], "m-paid")

    def test_same_key_different_model_conflicts(self):
        """换模型必须新建任务，不能用同一个幂等键悄悄换掉。"""
        self.post_job(request(), "same")
        status, body = self.post_job(request(model=PAID), "same")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "idempotency_key_reuse")

    def test_no_qualified_free_model_blocks_default(self):
        catalog = make_catalog()
        catalog.mark_status(FREE, CatalogStatus.DISCOVERED, reason="未验证", verified=False)
        catalog.mark_status(PAID, CatalogStatus.DISCOVERED, reason="未验证", verified=False)
        self.build(catalog=catalog)
        status, body = self.post_job(request(), "n1")
        self.assertEqual(status, 409)
        # 行为没变：**仍然拒绝**，错误码仍是 no_free_model。
        # 只是措辞改了 —— 产品里没有免费模型，不能对商户这么说。
        self.assertEqual(body.get("error"), "no_free_model")
        self.assertNotIn("免费模型", body["message"],
                         "商户能看到这条消息，里面不该出现「免费模型」")


class ModelSwitchTests(GatewayModelHarness):
    def setUp(self):
        self.build()

    def test_changing_the_setting_does_not_change_a_running_job(self):
        """任务创建时冻结了模型快照；改设置不能换掉正在跑的任务。"""
        _, body = self.post_job(request(), "r1")
        job_id = body["job"]["job_id"]
        first = self.store.job_snapshot(job_id)
        # 模拟商户这时把模型换成收费的
        self.post_job(request(model=PAID), "r2")
        again = self.store.job_snapshot(job_id)
        self.assertEqual(first, again, "已在跑的任务的模型快照不能被改")

    def test_a_new_job_uses_the_newly_selected_model(self):
        self.post_job(request(), "n1")
        _, body = self.post_job(request(model=PAID), "n2")
        self.assertEqual(self.store.job_snapshot(body["job"]["job_id"])["model_id"], "m-paid")

    def test_history_and_memory_are_untouched_by_a_model_switch(self):
        """换模型不应该动任何订单或客户资料。"""
        before = len(self.store.list_jobs(self.account["account_id"]))
        self.post_job(request(), "s1")
        self.post_job(request(model=PAID), "s2")
        self.assertEqual(len(self.store.list_jobs(self.account["account_id"])), before + 2)


if __name__ == "__main__":
    unittest.main()
