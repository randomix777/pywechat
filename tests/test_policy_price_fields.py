"""单价进策略配置。

## 为什么这一层必须有

上一轮把美元计价接进了网关，但单价**无处可配** —— 策略里没有这两个字段，
所以运行时永远读到 ``None``，走的还是固定额度回退。那样美元定价就是死代码。

## 0 和负数为什么算"没配"

免费档位已经焊死，把单价配成 0 **不会**让模型免费，只会退回固定额度。
如果这里放过 0，运营会看到"我配了 0 怎么还在扣钱"，而真实原因是它被当成了
没配 —— 那比明确退回固定额度更难排查。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.model_catalog import (  # noqa: E402
    CatalogStore,
    OperatorModelPolicy,
    PricingClass,
    _read_price,
    build_catalog,
)
from pyweixin.gateway import _optional_price  # noqa: E402


def _policy(**kwargs):
    base = {"alias": "m", "provider_id": "openrouter", "model_id": "vendor/model",
            "pricing_class": PricingClass.METERED, "cost_units_per_job": 1}
    base.update(kwargs)
    return OperatorModelPolicy.from_dict(base)


class PriceParsingTests(unittest.TestCase):
    def test_a_real_price_is_read(self):
        policy = _policy(input_price_per_million=0.003, output_price_per_million=0.015)
        self.assertAlmostEqual(policy.input_price_per_million, 0.003)
        self.assertAlmostEqual(policy.output_price_per_million, 0.015)

    def test_a_missing_price_is_none(self):
        policy = _policy()
        self.assertIsNone(policy.input_price_per_million)
        self.assertIsNone(policy.output_price_per_million)

    def test_zero_and_negative_mean_unset_not_free(self):
        """免费档位已焊死，0 单价不会让模型免费 —— 所以当成没配才可预期。"""
        policy = _policy(input_price_per_million=0, output_price_per_million=-1)
        self.assertIsNone(policy.input_price_per_million)
        self.assertIsNone(policy.output_price_per_million)

    def test_a_malformed_price_is_none(self):
        for raw in ("abc", "", [], {}, None, "nan"):
            with self.subTest(raw=raw):
                self.assertIsNone(_read_price(raw))

    def test_the_two_prices_are_independent(self):
        """只配一个也允许；网关那边两个都有效才会用，缺一个就退回固定额度。"""
        policy = _policy(input_price_per_million=0.003)
        self.assertIsNotNone(policy.input_price_per_million)
        self.assertIsNone(policy.output_price_per_million)


class RoundTripTests(unittest.TestCase):
    def test_a_price_survives_save_and_load(self):
        """配置存下去再读出来必须一样 —— 运营改一次不能悄悄丢。"""
        original = _policy(input_price_per_million=0.003, output_price_per_million=0.015)
        restored = OperatorModelPolicy.from_dict(original.to_dict())
        self.assertEqual(restored.input_price_per_million, original.input_price_per_million)
        self.assertEqual(restored.output_price_per_million, original.output_price_per_million)

    def test_an_unset_price_stays_unset(self):
        restored = OperatorModelPolicy.from_dict(_policy().to_dict())
        self.assertIsNone(restored.input_price_per_million)
        self.assertIsNone(restored.output_price_per_million)


class BillingSnapshotTests(unittest.TestCase):
    """网关从 billing_snapshot 取价，所以那里必须真的带上。"""

    def _entry(self, policy):
        store = CatalogStore()
        store.load_policies([policy.to_dict()])
        store.catalog = build_catalog(
            [{"provider_id": "openrouter", "name": "OpenRouter", "connected": True,
              "models": [{"model_id": "vendor/model", "display_name": "V", "context_limit": 1,
                          "output_limit": 1, "capabilities": {}, "upstream_cost": {},
                          "upstream_status": "live"}]}],
            {policy.alias: policy},
        )
        return store.catalog.entry(policy.alias)

    def test_the_price_reaches_the_billing_snapshot(self):
        policy = _policy(input_price_per_million=0.003, output_price_per_million=0.015)
        snapshot = self._entry(policy).billing_snapshot()
        self.assertEqual(_optional_price(snapshot, "input_price_per_million"), 0.003)
        self.assertEqual(_optional_price(snapshot, "output_price_per_million"), 0.015)

    def test_an_unset_price_is_not_downstream_as_zero(self):
        """下发 0 会让人以为配过了 —— 网关那边 0 的含义是"没配"。"""
        policy = _policy()
        snapshot = self._entry(policy).billing_snapshot()
        self.assertIsNone(snapshot["input_price_per_million"])
        self.assertIsNone(snapshot["output_price_per_million"])


class ClientVisibilityTests(unittest.TestCase):
    def test_the_price_is_not_exposed_to_clients(self):
        """上游单价是成本结构，不能让商户看到 —— 他只需要知道自己扣多少。"""
        policy = _policy(input_price_per_million=0.003, output_price_per_million=0.015)
        store = CatalogStore()
        store.load_policies([policy.to_dict()])
        store.catalog = build_catalog(
            [{"provider_id": "openrouter", "name": "OpenRouter", "connected": True,
              "models": [{"model_id": "vendor/model", "display_name": "V", "context_limit": 1,
                          "output_limit": 1, "capabilities": {}, "upstream_cost": {},
                          "upstream_status": "live"}]}],
            {policy.alias: policy},
        )
        store.catalog.mark_status(policy.alias, "selectable", verified=True)
        import json

        blob = json.dumps(store.catalog.for_client(), ensure_ascii=False)
        self.assertNotIn("0.003", blob)
        self.assertNotIn("price_per_million", blob)


if __name__ == "__main__":
    unittest.main()