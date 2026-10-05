"""用 OpenRouter 的**真实**定价数据验证计价。

## 为什么必须用真实数据

单位这件事，靠猜必错。OpenRouter 的 ``/api/v1/models`` 实际返回：

    "pricing": {"prompt": "2e-06", "completion": "1e-05"}

这是**每个 token** 多少美元，也就是**每百万 2 美元 / 10 美元**。
按「每百万 2e-06 美元」去读会**少算 100 万倍** —— 而少算意味着
平台每一次调用都在倒贴钱，而且不会报错。

这个错误靠人眼是发现不了的：数字看起来仍然是个很小的正数，
只是小了一百万倍。所以这里把真实返回原样钉住。

## 真实数据里的两类陷阱（2026-10 取自线上）

* ``"prompt": "0"`` —— 29 个模型是 0 价（多为免费层）。按 0 结算等于白送。
* ``"prompt": "-1"`` —— router 类模型（``openrouter/auto-beta``、
  ``nvidia/switchyard``）用 -1 表示"按实际路由计费"，不是负数。
  照算会得出**负成本**，商户额度会反过来增加。

两种都必须当「未知」退回固定额度。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.pricing import (  # noqa: E402
    US_PER_MILLION,
    merchant_micro_usd_for,
    price_from_mapping,
    settle_micro_usd_for,
)

#: 线上原样返回的三条（2026-10）。
REAL_SONNET = {"id": "anthropic/claude-sonnet-5.5",
               "pricing": {"prompt": "2e-06", "completion": "1e-05"}}
REAL_O1_PRO = {"id": "openai/o1-pro",
               "pricing": {"prompt": "0.00015", "completion": "0.0006"}}
REAL_FREE = {"id": "apodex/apodex-1.1-mini:free",
             "pricing": {"prompt": "0", "completion": "0"}}
REAL_ROUTER = {"id": "openrouter/auto-beta",
               "pricing": {"prompt": "-1", "completion": "-1"}}


class UnitTests(unittest.TestCase):
    """这一组盯的是「每 token」还是「每百万」。"""

    def test_per_token_prices_become_per_million(self):
        price = price_from_mapping(REAL_SONNET)
        self.assertIsNotNone(price)
        self.assertAlmostEqual(price.input_per_million, 2.0,
                               msg="2e-06 美元/token 应当是每百万 2 美元，不是 2e-06")
        self.assertAlmostEqual(price.output_per_million, 10.0)

    def test_reading_it_as_per_million_would_undercharge_a_millionfold(self):
        """把每 token 的单价当成每百万，会少算 100 万倍。

        这个错误不会报错：结果仍然是个很小的正数，
        只是小了一百万倍。平台每次都倒贴钱，而商户看到的
        是一个「合理的小数字」，不会发现哪里不对。
        """
        tokens = 1000
        per_token = 2e-06                                    # OpenRouter 给的
        per_million = per_token * US_PER_MILLION              # 我们对外统一用的
        right = tokens * per_token                           # 对：直接按每 token 单价
        wrong = tokens * per_token / US_PER_MILLION          # 错：当成每百万单价
        self.assertAlmostEqual(right, 0.002)
        self.assertAlmostEqual(wrong, 2e-09)
        self.assertAlmostEqual(right / wrong, 1_000_000)

    def test_the_cost_is_in_the_right_ballpark(self):
        price = price_from_mapping(REAL_SONNET)
        units = settle_micro_usd_for(1000, 200, price)
        # \u771f\u5b9e\uff1a$0.004 \u4e0a\u6e38 \u2192 10 \u500d \u2192 $0.04 \u5546\u6237\u8d26\u5355
        self.assertEqual(units, 40_000, "应该是 40000 微美元（$0.04）")


class UnknownPriceTests(unittest.TestCase):
    """真实数据里两类必须当「未知」的条目。"""

    def test_a_zero_price_is_not_free(self):
        """按 0 结算等于白送一次解析。"""
        self.assertIsNone(price_from_mapping(REAL_FREE))

    def test_a_negative_price_is_a_router_marker_not_a_discount(self):
        """-1 表示按实际路由计费。照算会得出负成本，商户额度反而增加。"""
        self.assertIsNone(price_from_mapping(REAL_ROUTER))

    def test_a_mixed_zero_and_negative_entry_is_refused(self):
        self.assertIsNone(
            price_from_mapping({"pricing": {"prompt": "2e-06", "completion": "-1"}})
        )


class NoModelPaysTheMerchantTests(unittest.TestCase):
    def test_a_pricey_model_still_costs_the_merchant_more_not_less(self):
        price = price_from_mapping(REAL_O1_PRO)
        self.assertIsNotNone(price)
        units = settle_micro_usd_for(1000, 200, price)
        self.assertGreater(units, 0, "最贵的模型也不能算出非正费用")

    def test_the_merchant_pays_ten_times_the_vendor(self):
        """1 美元上游 = 10 美元账单，这是商家定���的。"""
        price = price_from_mapping(REAL_SONNET)
        vendor_usd = 1000 * 2e-06 + 200 * 1e-05
        units = merchant_micro_usd_for(vendor_usd)
        # 向上取整：平台已经花了钱，小数部分让利等于每次都贴钱。
        self.assertEqual(units, math.ceil(vendor_usd * 10 * US_PER_MILLION))


class PriceSpreadTests(unittest.TestCase):
    def test_cheap_and_pricey_models_are_actually_told_apart(self):
        """这就是放弃「额度」口径换美元的意义：价格差异必须看得见。"""
        cheap = settle_micro_usd_for(1000, 200, price_from_mapping(REAL_SONNET))
        dear = settle_micro_usd_for(1000, 200, price_from_mapping(REAL_O1_PRO))
        # o1-pro $150/$600 每百万 vs sonnet $2/$10 每百万 —— 真实差 67.5 倍。
        self.assertGreater(dear, cheap * 50,
                           "价格差几十倍，费用应该也差得开")

    def test_an_unknown_price_falls_back_to_a_fixed_amount(self):
        """单价读不到时不能按 0 结算 —— 调用方退回固定额度。"""
        self.assertIsNone(settle_micro_usd_for(1000, 200, None))


if __name__ == "__main__":
    unittest.main()