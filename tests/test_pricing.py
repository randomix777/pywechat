"""额度与美元换算。

## 为什么要把这些单独钉住

这个模块算的是**钱**。两处最容易出错的地方：

1. **单价未知时编一个。** 结果就是按错的价格扣商户的钱，而且商户看不出来 ——
   账面上"扣了 1 个额度"，实际可能是 0.0001 个。
2. **向上取整被"优化"掉。** 平台已经花了钱，小数部分让利等于每次都在贴钱。

所以：不知道单价就返回 ``None``，让调用方自己决定；取整必须向上，最低 1。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.pricing import (  # noqa: E402
    MERCHANT_UNITS_PER_VENDOR_USD,
    ModelPrice,
    estimate_input_tokens,
    estimate_units_for_request,
    merchant_units_for,
    price_from_mapping,
    vendor_cost_usd,
)


class RateTests(unittest.TestCase):
    def test_one_vendor_dollar_is_ten_merchant_units(self):
        """商家定的：商户买 1 美元额度，平台实际花 0.1 美元上游。"""
        self.assertEqual(MERCHANT_UNITS_PER_VENDOR_USD, 10.0)
        self.assertEqual(merchant_units_for(0.1), 1)
        self.assertEqual(merchant_units_for(1.0), 10)

    def test_the_rate_is_the_only_place_it_is_written(self):
        """换算率必须只有一个出处。要改就改这里，不能散进业务代码。"""
        import pyweixin.pricing as pricing

        source = Path(pricing.__file__).read_text(encoding="utf-8")
        # \u9664\u4e86\u5b9a\u4e49\u884c\u4e0e\u6d4b\u8bd5\u7528\u7684\u90a3\u4e00\u884c\uff0c\u4e0d\u5e94\u8be5\u8fd8\u6709\u5176\u4ed6 10
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "= 10.0" in stripped:
                continue
            if "MERCHANT_UNITS_PER_VENDOR_USD" in stripped:
                self.assertNotIn("10.0", stripped, "\u6362\u7b97\u7387\u88ab\u5199\u6b7b\u5728\u5176\u4ed6\u5730\u65b9\u4e86")


class RoundingTests(unittest.TestCase):
    def test_it_always_rounds_up(self):
        """\u5e73\u53f0\u5df2\u7ecf\u82b1\u4e86\u94b1\uff0c\u5c0f\u6570\u90e8\u5206\u8ba9\u5229\u7b49\u4e8e\u6bcf\u6b21\u90fd\u5728\u8d34\u94b1\u3002"""
        self.assertEqual(merchant_units_for(0.101), 2)
        self.assertEqual(merchant_units_for(0.1), 1)

    def test_a_real_analysis_is_never_free(self):
        """\u4e00\u6b21\u771f\u5b9e\u7684\u5206\u6790\u4e0d\u5e94\u8be5\u7b97\u51fa 0 \u989d\u5ea6\uff0c\u90a3\u7b49\u4e8e\u514d\u8d39\u3002"""
        self.assertEqual(merchant_units_for(0.000001), 1)
        self.assertEqual(merchant_units_for(0.0), 1)

    def test_negative_input_cannot_make_it_free_either(self):
        self.assertEqual(merchant_units_for(-5), 1)


class HonestWhenUnknownTests(unittest.TestCase):
    """\u4e0d\u77e5\u9053\u5355\u4ef7\u65f6\uff0c\u4e0d\u5f97\u7f16\u3002"""

    def test_a_missing_price_gives_none_not_zero(self):
        """\u8fd4\u56de 0 \u4f1a\u6084\u6084\u53d8\u6210\u514d\u8d39\u3002"""
        self.assertIsNone(estimate_units_for_request("\u4e00\u6761\u8ba2\u5355", None))

    def test_a_malformed_pricing_block_gives_none(self):
        self.assertIsNone(price_from_mapping({"pricing": {"prompt": "abc"}}))
        self.assertIsNone(price_from_mapping({"pricing": {}}))
        self.assertIsNone(price_from_mapping({}))
        self.assertIsNone(price_from_mapping(None))

    def test_a_zero_price_is_not_treated_as_a_real_price(self):
        """0 \u4ef7\u610f\u5473\u7740\u201c\u8fd9\u4e2a\u6a21\u578b\u4e0d\u8981\u94b1\u201d\uff0c\u4e0d\u662f\u771f\u7684\u5355\u4ef7\u3002"""
        self.assertIsNone(price_from_mapping({"pricing": {"prompt": "0", "completion": "0"}}))

    def test_a_real_pricing_block_is_read(self):
        price = price_from_mapping({
            "id": "vendor/model",
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        })
        self.assertIsNotNone(price)
        self.assertEqual(price.model, "vendor/model")
        self.assertAlmostEqual(price.input_per_million, 0.000003)


class TokenEstimationTests(unittest.TestCase):
    def test_chinese_and_english_are_counted_differently(self):
        """中文一个字约一个 token，英文四个字符约一个。"""
        chinese = estimate_input_tokens("\u8ba2\u4e00\u7bb1\u82b1\u5de5")
        english = estimate_input_tokens("abcdefgh")
        self.assertGreater(chinese, 0)
        self.assertEqual(english, 2)

    def test_empty_text_is_zero(self):
        self.assertEqual(estimate_input_tokens(""), 0)
        self.assertEqual(estimate_input_tokens(None), 0)

    def test_it_is_documented_as_an_estimate(self):
        """\u771f\u5b9e token \u6570\u53ea\u6709\u4e0a\u6e38\u77e5\u9053\uff1b\u8fd9\u91cc\u4e0d\u80fd\u88c5\u4f5c\u7cbe\u786e\u503c\u3002"""
        self.assertEqual(estimate_input_tokens("a"), 1)


class FrozenPriceTests(unittest.TestCase):
    def test_cost_uses_per_million_prices(self):
        # 单价是「每百万 token 多少美元」。100 万 token x $0.003/百万 = $0.003。
        # 把这两者混了（当成「每个 token $0.003」），就会把成本算错 100 万倍。
        self.assertAlmostEqual(vendor_cost_usd(1_000_000, 0, 0.003, 0), 0.003, places=9)
        self.assertAlmostEqual(vendor_cost_usd(0, 1_000_000, 0, 0.015), 0.015, places=9)
        # 两种 token 合计、不取整、不回退到零。
        self.assertAlmostEqual(vendor_cost_usd(1_000_000, 1_000_000, 0.003, 0.015), 0.018, places=9)
        # 真实体量：几千 token 的成本还是小数额度，不是。。。
        self.assertLess(vendor_cost_usd(3_000, 800, 0.003, 0.015), 0.001)

    def test_negative_tokens_cannot_produce_money(self):
        self.assertEqual(vendor_cost_usd(-100, -100, 1.0, 1.0), 0.0)

    def test_a_realistic_parse_costs_the_merchant_a_sane_amount(self):
        """\u4e00\u6b21\u771f\u5b9e\u7684\u8ba2\u5355\u5206\u6790\uff1a\u51e0\u5343 token \u3001\u51e0\u5341\u7f8e\u5206\u4e0a\u6e38\u3002"""
        price = ModelPrice(model="m", input_per_million=0.000003, output_per_million=0.000015)
        units = estimate_units_for_request("\u5ba2\u6237\u8981 3 \u7bb1\u82b1\u5de5\uff0c\u7ea6 300 \u5143", price)
        self.assertIsNotNone(units)
        self.assertGreaterEqual(units, 1)
        # \u51e0\u5343 token \u7684\u771f\u5b9e\u6210\u672c\u5e94\u8be5\u843d\u5728\u4e00\u4e2a\u5c0f\u6570\u989d\u5ea6\u4e0a\uff0c\u800c\u4e0d\u662f\u51e0\u5341\u4e2a\u3002
        self.assertLess(units, 20, "\u9884\u4f30\u660e\u663e\u504f\u9ad8\uff0c\u4f1a\u628a\u5546\u6237\u591a\u6536\u8d39")


class MarginTests(unittest.TestCase):
    def test_the_merchant_pays_ten_times_the_vendor_cost(self):
        """\u5b88\u4f4f\u8fd9\u4e2a\u500d\u6570\u624d\u662f\u5546\u6a21\uff1a\u5546\u6237\u652f 10 \u4e2a\u989d\u5ea6\uff0c\u5e73\u53f0\u82b1 1 \u4e2a\u989d\u5ea6\u7684\u94b1\u3002"""
        vendor_usd = 0.05
        units = merchant_units_for(vendor_usd)
        self.assertEqual(units, int(vendor_usd * 10) + 1)  # \u5411\u4e0a\u53d6\u6574


if __name__ == "__main__":
    unittest.main()