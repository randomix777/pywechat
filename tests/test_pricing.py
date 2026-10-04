"""美元计价与换算。

## 为什么要把这些单独钉住

这个模块算的是**钱**，而且用的是**整数微美元**。两处最容易出错：

1. **单价未知时编一个。** 结果就是按错的价格扣商户的钱，而且商户看不出来 ——
   账面上"扣了 0.0001 美元"，实际可能是 0.000001。
2. **浮点记账。** 金额一旦用 float 存，累加几万次后就会出现分位误差，
   商户会莫名少一分钱，而且**永远查不出来是哪里错的**。

另外还钉住一件事：换算率必须只有一个出处，写死在别处等于这份定价失效。

## 为什么是微美元而不是美元

因为一次真实解析的成本常常远小于 1 美分。如果直接按美元记整数，
所有解析都是 0 美元 —— 等于全免费。微美元既保留了整数记账的纪律，
又细到能区分真实价格差异。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.pricing import (  # noqa: E402
    MERCHANT_USD_PER_VENDOR_USD,
    US_PER_USD,
    ModelPrice,
    estimate_input_tokens,
    estimate_micro_usd_for_request,
    format_usd,
    merchant_micro_usd_for,
    price_from_mapping,
    vendor_cost_usd,
)


class RateTests(unittest.TestCase):
    def test_one_vendor_dollar_ten_merchant_dollars(self):
        """商家定的：商户买 1 美元额度，平台实际花 0.1 美元上游。"""
        self.assertEqual(MERCHANT_USD_PER_VENDOR_USD, 10.0)
        self.assertEqual(merchant_micro_usd_for(0.1), 1 * US_PER_USD)
        self.assertEqual(merchant_micro_usd_for(1.0), 10 * US_PER_USD)

    def test_the_rate_is_written_in_exactly_one_place(self):
        """换算率必须只有一个出处 —— **数值**只能有一处。

        函数签名里引用常量（``rate: float = MERCHANT_USD_PER_VENDOR_USD``）
        是正确写法，不算复制。要防的是有人直接把 10 写进某个函数体或者
        另一个模块，那样改定价时只会改到一处，另一处就悄悄失效了。
        """
        import pyweixin.pricing as pricing

        source = Path(pricing.__file__).read_text(encoding="utf-8")
        definitions = sum(
            1 for line in source.splitlines()
            if line.strip().startswith("MERCHANT_USD_PER_VENDOR_USD = ")
        )
        self.assertEqual(definitions, 1, "换算率的定义应当有且只有一处")

        # 去掉常量自身的定义行与文档注释，剩下的地方不该再出现这个数值。
        body = [
            line for line in source.splitlines()
            if not line.strip().startswith(("#", "MERCHANT_USD_PER_VENDOR_USD = "))
        ]
        for line in body:
            self.assertNotIn("10.0", line, "10 这个倍数被写死在别处了")


class MicroDollarTests(unittest.TestCase):
    def test_money_is_never_stored_as_a_float(self):
        """金额必须是整数。float 记账在累加几万次后会出分位误差。"""
        for usd in (0.0, 0.1, 0.0133, 0.0666, 1.0):
            self.assertIsInstance(merchant_micro_usd_for(usd), int)

    def test_it_rounds_up_but_never_has_a_floor(self):
        """向上取整是对的；但**不能**再加"最低 1" —— 微美元已经足够细，
        加了会把价格差异压平（那正是放弃额度口径换来的好处）。"""
        self.assertEqual(merchant_micro_usd_for(0.0000001), 1)
        # 一分钱都不到的真实解析，必须能算出非零且有差异。
        cheap = merchant_micro_usd_for(0.000012)
        dear = merchant_micro_usd_for(0.0666)
        self.assertGreater(cheap, 0)
        self.assertGreater(dear, cheap * 100)

    def test_different_prices_produce_different_charges(self):
        """这一条盯的是放弃额度口径换来的好处：价格差异必须看得见。"""
        prompt = "客户：要 3 箱花工，约 300 元"
        charges = [
            estimate_micro_usd_for_request(
                prompt, ModelPrice(model="m", input_per_million=pi, output_per_million=po))
            for pi, po in ((0.003, 0.015), (0.3, 1.2), (3.0, 15.0), (15.0, 75.0))
        ]
        self.assertTrue(all(c and c > 0 for c in charges), f"有一笔算出了 0：{charges}")
        self.assertEqual(charges, sorted(charges), "价钱越贵，扣的越多")
        self.assertGreater(len(set(charges)), 1, "四个价钱差 2000 倍的模型扣了同一个金额")

    def test_negative_input_cannot_make_it_free_or_negative(self):
        self.assertEqual(merchant_micro_usd_for(-5), 0)


class HonestWhenUnknownTests(unittest.TestCase):
    """不知道单价时，不许编。"""

    def test_a_missing_price_gives_none_not_zero(self):
        """返回 0 会悄悄变成免费 —— 那是最坏的失败方式，因为它看起来成功。"""
        self.assertIsNone(estimate_micro_usd_for_request("一条订单", None))

    def test_a_malformed_pricing_block_gives_none(self):
        self.assertIsNone(price_from_mapping({"pricing": {"prompt": "abc"}}))
        self.assertIsNone(price_from_mapping({"pricing": {}}))
        self.assertIsNone(price_from_mapping({}))
        self.assertIsNone(price_from_mapping(None))

    def test_a_zero_price_is_not_treated_as_a_real_price(self):
        """0 价意味着"这个模型不要钱"，不是真的单价。"""
        self.assertIsNone(price_from_mapping({"pricing": {"prompt": "0", "completion": "0"}}))

    def test_a_real_pricing_block_is_read(self):
        price = price_from_mapping({
            "id": "vendor/model",
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        })
        self.assertIsNotNone(price)
        self.assertEqual(price.model, "vendor/model")
        self.assertAlmostEqual(price.input_per_million, 0.000003)


class VendorCostTests(unittest.TestCase):
    def test_cost_uses_per_million_prices(self):
        """单价是"每百万 token 多少美元"。100 万 token × $0.003/百万 = $0.003。
        把这两者混了（当成"每个 token $0.003"）会把成本算错 100 万倍。"""
        self.assertAlmostEqual(vendor_cost_usd(1_000_000, 0, 0.003, 0), 0.003, places=9)
        self.assertAlmostEqual(vendor_cost_usd(0, 1_000_000, 0, 0.015), 0.015, places=9)
        self.assertAlmostEqual(vendor_cost_usd(1_000_000, 1_000_000, 0.003, 0.015), 0.018, places=9)

    def test_negative_tokens_cannot_produce_money(self):
        self.assertEqual(vendor_cost_usd(-100, -100, 1.0, 1.0), 0.0)


class TokenEstimationTests(unittest.TestCase):
    def test_chinese_and_english_are_counted_differently(self):
        chinese = estimate_input_tokens("订一箱花工")
        english = estimate_input_tokens("abcdefgh")
        self.assertGreater(chinese, 0)
        self.assertEqual(english, 2)

    def test_empty_text_is_zero(self):
        self.assertEqual(estimate_input_tokens(""), 0)
        self.assertEqual(estimate_input_tokens(None), 0)


class DisplayTests(unittest.TestCase):
    """给商户看的金额。"""

    def test_sub_cent_amounts_are_not_shown_as_zero(self):
        """显示成 0.00 会让商户以为自己没被扣钱、或者被系统吞了。"""
        self.assertEqual(format_usd(12), "$0.000012")
        self.assertEqual(format_usd(120), "$0.000120")

    def test_normal_amounts_are_two_decimals(self):
        self.assertEqual(format_usd(1_000_000), "$1.00")
        self.assertEqual(format_usd(2_500_000), "$2.50")

    def test_zero_is_shown_as_zero(self):
        self.assertEqual(format_usd(0), "$0.00")

    def test_a_negative_balance_does_not_display_as_negative_money(self):
        """欠费时不能显示成负数金额 —— 那会让商户以为自己有余额。"""
        self.assertEqual(format_usd(-500_000), "$0.00")


if __name__ == "__main__":
    unittest.main()