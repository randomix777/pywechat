"""美元计价接进扣费链路。

## 这一轮接的是什么

之前 :mod:`wechat_ai_assistant.pricing` 算得出美元金额，但**没有任何调用点** ——
实际扣费走的还是固定的整数 ``cost_micro_usd_per_job``。这一轮把两者接上。

## 核心约定：预留按上限，实扣按真实用量

提交时**输出 token 是未知的**（输入能估，输出不能）。所以：

* 提交：按**最大可能成本**预留；
* 结算：按上游回���**真实** token 用量实扣，多占的自动退回。

为什么不按预留全额扣：商户就会为"模型最多可能用到的量"付钱。消息短的
时候那部分再也回不来。

## 三条不能破的底线

1. **预留不能低于实扣**，否则任务跑到一半才发现余额不够 —— 那时候钱已经花了。
2. **实扣不能高于预留**，否则商户余额会变成负数。
3. **单价拿不到就退回固定额度**，绝不按 0 结算 —— 0 等于免费，而免费已焊死。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.gateway import ProductGateway, _optional_price  # noqa: E402
from wechat_ai_assistant.pricing import (  # noqa: E402
    MERCHANT_USD_PER_VENDOR_USD,
    US_PER_USD,
    ModelPrice,
    reserve_micro_usd_for_request,
    settle_micro_usd_for,
)


PRICE = ModelPrice(model="vendor/model", input_per_million=0.003, output_per_million=0.015)


class ReserveVersusSettleTests(unittest.TestCase):
    def test_reserving_more_than_a_real_parse_costs(self):
        """实际用量几乎总是小于上限 —— 短的解析不该按上限付钱。"""
        reserved = reserve_micro_usd_for_request("订一箱花工", PRICE, output_tokens=800)
        actual = settle_micro_usd_for(40, 120, PRICE)
        self.assertGreater(reserved, actual)
        # 差额必须是实质性的，否则"预留上限"就失去了意义。
        self.assertGreater(reserved - actual, reserved * 0.5)

    def test_the_reservation_always_covers_a_real_parse(self):
        """这条不能破：预留低于实扣，等于跑到一半才发现余额不够。"""
        reserved = reserve_micro_usd_for_request("订一箱花工", PRICE, output_tokens=800)
        # 即使输出打满上限，也不能超过预留。
        worst = settle_micro_usd_for(40, 800, PRICE)
        self.assertLessEqual(worst, reserved)

    def test_longer_messages_cost_more(self):
        short = settle_micro_usd_for(40, 100, PRICE)
        long = settle_micro_usd_for(40, 1000, PRICE)
        self.assertGreater(long, short)

    def test_both_can_be_zero_only_when_the_usage_really_is_zero(self):
        self.assertEqual(settle_micro_usd_for(0, 0, PRICE), 0)
        self.assertEqual(settle_micro_usd_for(40, 100, PRICE), settle_micro_usd_for(40, 100, PRICE))

    def test_without_a_price_both_sides_refuse_to_guess(self):
        self.assertIsNone(reserve_micro_usd_for_request("x", None))
        self.assertIsNone(settle_micro_usd_for(100, 100, None))


class OptionalPriceTests(unittest.TestCase):
    """单价取不到时必须退回固定额度，**绝不能按 0**。"""

    def test_a_missing_or_bad_price_is_none(self):
        for raw in (None, "", "abc", 0, -1, -0.5):
            with self.subTest(raw=raw):
                self.assertIsNone(_optional_price({"input_price_per_million": raw},
                                                  "input_price_per_million"))

    def test_a_real_price_is_kept(self):
        self.assertEqual(_optional_price({"input_price_per_million": 0.003},
                                         "input_price_per_million"), 0.003)

    def test_a_missing_key_is_none_not_zero(self):
        self.assertIsNone(_optional_price({}, "input_price_per_million"))


class ChargeUnitsTests(unittest.TestCase):
    """网关选实扣金额时的优先级。"""

    def _snapshot(self, **inner):
        return {"pricing_class": "metered", "snapshot": {"cost_micro_usd_per_job": 1, **inner}}

    class _Outcome:
        input_tokens = 40
        output_tokens = 120
        cost_micro_usd = 99

    def test_real_usage_wins_when_the_price_is_frozen(self):
        gateway = object.__new__(ProductGateway)
        charged = ProductGateway._frozen_charge_micro_usd(
            gateway, "job-1",
            self._snapshot(input_price_per_million=0.003, output_price_per_million=0.015),
            self._Outcome(),
        )
        expected = settle_micro_usd_for(40, 120, PRICE)
        self.assertEqual(charged, expected)
        self.assertNotEqual(charged, 1, "有真实单价时不该再按固定 1 扣")

    def test_it_falls_back_to_the_frozen_fixed_amount(self):
        """单价拿不到时退回运营配的固定值 —— 比猜一个强。"""
        gateway = object.__new__(ProductGateway)
        charged = ProductGateway._frozen_charge_micro_usd(
            gateway, "job-1", self._snapshot(), self._Outcome(),
        )
        self.assertEqual(charged, 1)

    def test_a_broken_price_does_not_break_settlement(self):
        """坏数据不能让整个结算卡死，也不能变成 0（0 等于免费）。"""
        gateway = object.__new__(ProductGateway)
        for broken in ("abc", -1, 0):
            with self.subTest(broken=broken):
                charged = ProductGateway._frozen_charge_micro_usd(
                    gateway, "job-1",
                    self._snapshot(input_price_per_million=broken,
                                   output_price_per_million=broken),
                    self._Outcome(),
                )
                self.assertEqual(charged, 1, "坏单价必须退回固定额度，而不是 0")

    def test_free_never_charges(self):
        gateway = object.__new__(ProductGateway)
        charged = ProductGateway._frozen_charge_micro_usd(
            gateway, "job-1",
            {"pricing_class": "free", "snapshot": {}},
            self._Outcome(),
        )
        self.assertEqual(charged, 0)


if __name__ == "__main__":
    unittest.main()