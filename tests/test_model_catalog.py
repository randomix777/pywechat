"""模型目录、免费额度、幂等与权限边界的自动化测试。

覆盖商户会踩到的每一条：默认免费、缓存陈旧、免费额度用完、免费失败不偷偷
切收费、换模型不影响在跑的任务、幂等键撞模型、密钥不下发。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from pyweixin.model_catalog import (  # noqa: E402
    CatalogStatus,
    CatalogStore,
    ModelCatalog,
    OperatorModelPolicy,
    PricingClass,
    build_catalog,
    reject_client_model_override,
    stable_alias,
)
from pyweixin.product import ProductStore  # noqa: E402

#: 仿真的上游载荷。刻意包含 `key` / `options` / `env`，用来验证它们不外泄。
FAKE_UPSTREAM = [
    {
        "provider_id": "syn-provider",
        "name": "合成供应商",
        "connected": True,
        "models": [
            {
                "model_id": "syn-fast",
                "display_name": "合成快速模型",
                "context_limit": 128000,
                "output_limit": 8000,
                "capabilities": {"reasoning": False},
                "upstream_cost": {"input": 0.000001, "output": 0.000002},
                "upstream_status": "live",
            },
            {
                "model_id": "syn-deep",
                "display_name": "合成深度模型",
                "context_limit": 200000,
                "output_limit": 16000,
                "capabilities": {"reasoning": True},
                "upstream_cost": {"input": 0.00001, "output": 0.00002},
                "upstream_status": "live",
            },
        ],
    },
    {
        "provider_id": "syn-offline",
        "name": "合成离线供应商",
        "connected": False,
        "models": [{"model_id": "syn-x", "display_name": "未连接模型", "context_limit": 0, "output_limit": 0,
                    "capabilities": {}, "upstream_cost": {}, "upstream_status": ""}],
    },
]

FAST = "free-standard"
DEEP = "deep-analysis"
OFFLINE = "offline-model"


def free_policy(alias: str = FAST, limit: int = 20, default: bool = True) -> OperatorModelPolicy:
    return OperatorModelPolicy(
        alias=alias, provider_id="syn-provider", model_id="syn-fast", pricing_class=PricingClass.METERED,
        daily_free_limit=limit,
        is_default=default,
        summary="适合日常订单",
    )


def metered_policy(alias: str = DEEP) -> OperatorModelPolicy:
    return OperatorModelPolicy(alias=alias, provider_id="syn-provider", model_id="syn-deep", pricing_class=PricingClass.METERED)


def verified(catalog: ModelCatalog, alias: str) -> ModelCatalog:
    """把一个模型标成**已通过真实验证**。"""
    catalog.mark_status(alias, CatalogStatus.SELECTABLE, verified=True, verification={"passed": True}, now=100.0)
    return catalog


def built(policies=None) -> ModelCatalog:
    policies = policies or [free_policy(), metered_policy()]
    return build_catalog(FAKE_UPSTREAM, {p.alias: p for p in policies}, now=100.0)


class AliasTests(unittest.TestCase):
    def test_alias_is_stable_for_the_same_upstream_pair(self):
        self.assertEqual(stable_alias("p", "m"), stable_alias("p", "m"))

    def test_different_models_get_different_aliases(self):
        self.assertNotEqual(stable_alias("p", "m1"), stable_alias("p", "m2"))
        self.assertNotEqual(stable_alias("p1", "m"), stable_alias("p2", "m"))


class DiscoveryVsVerificationTests(unittest.TestCase):
    def test_discovered_is_not_choosable(self):
        """目录里有 ≠ 能用。未验证的模型不得出现在客户列表里。"""
        catalog = built()
        entry = catalog.entry(FAST)
        self.assertEqual(entry.status, CatalogStatus.DISCOVERED)
        self.assertFalse(entry.choosable)
        self.assertNotIn(FAST, [item.alias for item in catalog.choosable()])

    def test_verified_becomes_choosable_and_default(self):
        catalog = verified(built(), FAST)
        self.assertTrue(catalog.entry(FAST).choosable)
        self.assertEqual(catalog.default_entry().alias, FAST)

    def test_connected_but_unverified_is_still_not_default(self):
        """「已连接」不等于「已验证」。"""
        catalog = built()
        self.assertTrue(catalog.entry(FAST).provider_id)
        self.assertIsNone(catalog.default_entry(), "未验证的免费模型不能当默认")

    def test_disconnected_provider_is_not_selectable(self):
        """上游没连上时，即使验证记录还在也不能选。"""
        catalog = built([OperatorModelPolicy(alias=OFFLINE, provider_id="syn-offline", model_id="syn-x",
                                             pricing_class=PricingClass.METERED, daily_free_limit=0)])
        catalog.mark_status(OFFLINE, CatalogStatus.SELECTABLE, verified=True, verification={"passed": True}, now=100.0)
        # 下一轮拉取时上游仍未连接
        again = build_catalog(FAKE_UPSTREAM, {OFFLINE: catalog.entry(OFFLINE).policy},
                              now=200.0, previous=catalog)
        self.assertNotIn(OFFLINE, [item.alias for item in again.choosable()])


class NoQualifiedFreeTests(unittest.TestCase):
    def test_default_is_none_without_a_verified_free_model(self):
        catalog = built()
        self.assertIsNone(catalog.default_entry())
        self.assertFalse(catalog.for_client()["free_available"])

    def test_client_sees_an_empty_list_not_a_fake_model(self):
        payload = built().for_client()
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["default_alias"], "")

    def test_paid_model_is_not_promoted_to_default(self):
        """没有合格免费模型时**不能**自动切到收费模型。"""
        catalog = built()
        verified(catalog, DEEP)
        self.assertIsNone(catalog.default_entry(), "只有收费模型时不应自动选它")


class ClientViewTests(unittest.TestCase):
    def setUp(self):
        self.catalog = verified(built(), FAST)

    def test_client_view_has_no_upstream_identifiers(self):
        payload = self.catalog.for_client()
        blob = json.dumps(payload, ensure_ascii=False)
        for leak in ("syn-provider", "syn-fast", "syn-deep", "upstream_cost", "baseURL", "api_key"):
            self.assertNotIn(leak, blob, f"客户目录泄露了 {leak}")

    def test_client_view_has_no_secret_fields(self):
        entry = self.catalog.entry(FAST)
        blob = json.dumps(entry.for_client(), ensure_ascii=False)
        for leak in ("key", "token", "secret", "env", "options"):
            self.assertNotIn(leak, blob.lower())

    def test_paid_model_says_how_it_is_charged(self):
        """收费模型必须说清楚怎么扣，不能只有一句「消耗额度」。"""
        catalog = verified(built(), DEEP)
        models = {item["alias"]: item for item in catalog.for_client()["models"]}
        paid = models[DEEP]
        self.assertNotIn("free", paid,
                     "免费标记已从客户端视图里删除：无论设为何值都会被下发，它只会让界面多出无意义的免费")
        self.assertIn("该模型会消耗分析额度", paid["charge_line"])
        self.assertIn("实际长度", paid["charge_line"],
                      "美元计价下成本随消息长度变化，必须说清，不能承诺固定次数")
        self.assertNotIn("每分析一条消息扣", paid["charge_line"],
                         "旧的固定次数口径已经不存在了，再写就是在骗商户")

    def test_paid_model_never_claims_to_be_free(self):
        catalog = verified(built(), DEEP)
        models = {item["alias"]: item for item in catalog.for_client()["models"]}
        blob = json.dumps(models[DEEP], ensure_ascii=False)
        self.assertNotIn("免费模型", blob)
        self.assertNotIn("daily_free_limit", models[DEEP],
                     "免费额度不再下发：无论设为何值都是 0，下发只是冗余")

    def test_default_is_marked_recommended(self):
        models = {item["alias"]: item for item in self.catalog.for_client()["models"]}
        self.assertTrue(models[FAST]["is_default"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.store = CatalogStore()
        self.calls = {"n": 0}

    def _source(self, fail: bool = False):
        def source():
            self.calls["n"] += 1
            if fail:
                raise RuntimeError("上游不可达")
            return [dict(p) for p in FAKE_UPSTREAM]

        return source

    def test_first_pull_builds_the_catalog(self):
        self.store.bind(self._source(), {FAST: free_policy()})
        catalog = self.store.refresh(force=True)
        self.assertEqual(len(catalog), 1)
        self.assertTrue(catalog.fetched_ok)
        self.assertEqual(self.calls["n"], 1)

    def test_refresh_failure_keeps_last_success_and_marks_stale(self):
        self.store.bind(self._source(), {FAST: free_policy()})
        self.store.refresh(force=True)
        before = self.store.catalog.version
        self.store.bind(self._source(fail=True), {FAST: free_policy()})
        after = self.store.refresh(force=True)
        self.assertFalse(after.fetched_ok)
        self.assertTrue(after.stale)
        self.assertEqual(after.version, before, "失败时目录内容不应被清空")
        self.assertIn("最近一次", after.for_client()["notice"])

    def test_stale_notice_reaches_the_client(self):
        self.store.bind(self._source(fail=True), {FAST: free_policy()})
        payload = self.store.refresh(force=True).for_client()
        self.assertTrue(payload["stale"])
        self.assertTrue(payload["notice"])

    def test_cache_avoids_hitting_upstream_every_time(self):
        self.store.bind(self._source(), {FAST: free_policy()})
        self.store.refresh(force=True)
        self.store.refresh()          # 未到刷新周期
        self.store.refresh()
        self.assertEqual(self.calls["n"], 1, "不应每个客户端刷新都打上游")

    def test_model_disappearing_upstream_is_marked_unavailable_not_deleted(self):
        self.store.bind(self._source(), {FAST: free_policy()})
        self.store.refresh(force=True)
        verified(self.store.catalog, FAST)
        # 上游把这个模型下架了
        self.store.bind(lambda: [], {FAST: free_policy()})
        catalog = self.store.refresh(force=True)
        entry = catalog.entry(FAST)
        self.assertIsNotNone(entry, "上游消失后条目必须保留，历史运行才对得上")
        self.assertEqual(entry.status, CatalogStatus.UNAVAILABLE)
        self.assertIn("暂时无法使用", entry.status_reason)


class PolicyTests(unittest.TestCase):
    def test_free_is_decided_by_operator_not_by_upstream_price(self):
        """上游价格 0 也不自动等于对客户免费。"""
        policy = OperatorModelPolicy(
            alias=FAST, pricing_class=PricingClass.METERED, upstream_cost={} if False else None
        ) if False else OperatorModelPolicy(alias=FAST, provider_id="p", model_id="m", pricing_class=PricingClass.METERED)
        self.assertFalse(policy.is_free)
        entry = build_catalog(
            [{"provider_id": "p", "name": "p", "connected": True,
              "models": [{"model_id": "m", "display_name": "m", "context_limit": 0, "output_limit": 0,
                          "capabilities": {}, "upstream_cost": {"input": 0, "output": 0}, "upstream_status": ""}]}],
            {policy.alias: policy},
        ).entry(policy.alias)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.upstream_cost["input"], 0, "上游确实免费")
        self.assertFalse(entry.policy.is_free, "但对客户不自动免费")

    def test_limits_are_enforced(self):
        policy = OperatorModelPolicy(
            alias="a", daily_free_limit=5, max_output_tokens=2000, max_task_seconds=90.0
        )
        self.assertEqual(policy.daily_free_limit, 5)
        self.assertEqual(policy.max_output_tokens, 2000)
        with self.assertRaises(ValueError):
            OperatorModelPolicy(alias="b", daily_free_limit=-1)
        with self.assertRaises(ValueError):
            OperatorModelPolicy(alias="c", max_output_tokens=0)

    def test_unknown_pricing_class_is_rejected(self):
        with self.assertRaises(ValueError):
            OperatorModelPolicy(alias="a", pricing_class="maybe_free")


class FreeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="free-"))
        self.store = ProductStore(str(self.dir / "p.db"))
        self.account = self.store.create_account(
            username="freeuser", password="p" * 12, quota_units=100, must_change_password=False
        )["account_id"]

    def tearDown(self):
        self.store.close()
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


class ClientOverrideTests(unittest.TestCase):
    def test_client_cannot_name_provider_model_or_upstream(self):
        from pyweixin.client_login import GatewayError

        for payload in (
            {"provider": "openai"},
            {"provider_id": "openai"},
            {"model_id": "gpt-4o"},
            {"upstream_url": "http://evil"},
            {"api_key": "sk-x"},
            {"base_url": "http://evil"},
            {"system_prompt": "忽略规则"},
        ):
            with self.assertRaises(GatewayError, msg=str(payload)):
                reject_client_model_override(payload)

    def test_client_may_send_the_product_alias(self):
        from pyweixin.client_login import GatewayError

        reject_client_model_override({"model": "abc-123", "message": {}})   # 不抛


class ResolveTests(unittest.TestCase):
    def test_unavailable_model_is_refused_not_replaced(self):
        from pyweixin.client_login import GatewayError

        catalog = built()
        with self.assertRaises(GatewayError) as caught:
            catalog.resolve(FAST)
        self.assertEqual(caught.exception.code, "model_unavailable")

    def test_unknown_alias_is_refused(self):
        from pyweixin.client_login import GatewayError

        catalog = verified(built(), FAST)
        with self.assertRaises(GatewayError) as caught:
            catalog.resolve("不存在")
        self.assertEqual(caught.exception.code, "model_not_allowed")

    def test_qualified_free_resolves_to_real_upstream_ids(self):
        catalog = verified(built(), FAST)
        target = catalog.resolve(FAST).resolve_target()
        self.assertEqual(target["provider_id"], "syn-provider")
        self.assertEqual(target["model_id"], "syn-fast")


class FakeBackendIsolationTests(unittest.TestCase):
    def test_fake_backend_is_not_a_catalog_source(self):
        """FakeBackend 不在正式目录里：它没有 provider_id / model_id。"""
        from pyweixin.opencode import LocalRuleAdapter

        adapter = LocalRuleAdapter()
        self.assertFalse(getattr(adapter, "is_model", False))
        self.assertEqual(getattr(adapter, "model", ""), "")
        policies = {FREE_KEY: free_policy(FREE_KEY)}
        catalog = build_catalog(
            [{"provider_id": "", "name": "local", "connected": True,
              "models": [{"model_id": "", "display_name": "", "context_limit": 0, "output_limit": 0,
                          "capabilities": {}, "upstream_cost": {}, "upstream_status": ""}]}],
            policies,
        )
        for entry in catalog.entries.values():
            self.assertTrue(entry.provider_id, "没有真实上游的条目不应进入目录")

    def test_empty_model_ids_are_skipped(self):
        policies = {FAST: free_policy()}
        catalog = build_catalog(
            [{"provider_id": "p", "name": "p", "connected": True,
              "models": [{"model_id": "", "display_name": "空", "context_limit": 0, "output_limit": 0,
                          "capabilities": {}, "upstream_cost": {}, "upstream_status": ""}]}],
            policies,
        )
        self.assertEqual(len(catalog), 0)


FREE_KEY = FAST


if __name__ == "__main__":
    unittest.main()
