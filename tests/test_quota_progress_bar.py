"""额度展示形式：**进度条 + 百分比**。

## 商户的原话

    类似chatgpt，额度给一个额度条，按百分比显示，不显示具体能做多少次

前一轮我做的是反的：把余额换算成「还够分析约 42 条消息」。

那个数不可靠：**一条消息常常不是一笔订单**。
商户的群里有的是「苹果两箱、砂糖桃三箱」——一条消息两笔订单。
换算出来的条数会让他照着错误的口径决定充多少。

百分比是认得的—— ChatGPT 就是这个样子。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.wizard import QuotaView  # noqa: E402


class PercentageTests(unittest.TestCase):
    def test_it_shows_remaining_percentage(self):
        text = QuotaView(logged_in=True, active=True, remaining=5,
                         total_micro_usd=8, used_micro_usd=3).headline()
        self.assertIn("剩余 62%", text)   # 5/8, rounded

    def test_reservations_count_as_used(self):
        """已接单尚未结算的额度也算已用 —— 否则进度条会跳回去。"""
        view = QuotaView(logged_in=True, active=True, remaining=4,
                         total_micro_usd=8, used_micro_usd=3, reserved_micro_usd=1)
        self.assertEqual(view.used_percent, 50)
        self.assertEqual(view.remaining_percent, 50)

    def test_it_is_clamped(self):
        """超兜的数据不能让条长超出条本身。"""
        self.assertEqual(
            QuotaView(logged_in=True, active=True, remaining=0,
                      total_micro_usd=8, used_micro_usd=99).used_percent, 100)


class NoTranslationTests(unittest.TestCase):
    """**不换算成条数。**"""

    def test_the_headline_never_mentions_messages_or_counts(self):
        for view in (
            QuotaView(logged_in=True, active=True, remaining=128, total_micro_usd=200, used_micro_usd=72),
            QuotaView(logged_in=True, active=True, remaining=2, total_micro_usd=8, used_micro_usd=7),
        ):
            with self.subTest(text=view.headline()):
                self.assertNotIn("条消息", view.headline())
                self.assertNotIn("还够分析", view.headline())

    def test_nothing_left_to_convert_on_the_view(self):
        self.assertFalse(hasattr(QuotaView, "messages_left"),
                         "换算逻辑已经不要了，留着就是死代码")

    def test_the_unit_cost_is_gone_from_the_quota_view(self):
        self.assertFalse(hasattr(QuotaView, "cost_micro_usd_per_job"),
                         "单价只是为了换算，换算取消就应该拆掉")


class HonestyTests(unittest.TestCase):
    def test_unlimited_plans_do_not_show_a_percentage(self):
        view = QuotaView(logged_in=True, active=True, remaining=5, total_micro_usd=0)
        self.assertTrue(view.unlimited)
        self.assertEqual(view.used_percent, 0)
        self.assertIn("不限", view.headline())

    def test_an_unknown_balance_is_not_mistaken_for_unlimited(self):
        view = QuotaView(logged_in=True, balance_known=False)
        self.assertFalse(view.unlimited)
        self.assertEqual(view.headline(), "额度未知，联网后自动刷新")

    def test_an_exhausted_quota_says_so_without_inventing_a_count(self):
        text = QuotaView(logged_in=True, active=True, remaining=0,
                         total_micro_usd=8, used_micro_usd=8).headline()
        self.assertIn("已用完", text)
        self.assertNotIn("还能再分析", text,
                         "充多少由商户决定，我们不能替他保证")

    def test_not_logged_in_and_expired_are_unaffected(self):
        self.assertEqual(QuotaView(logged_in=False).headline(), "未登录产品账号")
        self.assertEqual(QuotaView(logged_in=True, active=False).headline(),
                         "套餐已到期")


class WiringTests(unittest.TestCase):
    def test_the_client_reads_the_three_numbers_the_bar_needs(self):
        source = (ROOT / "src" / "pyweixin" / "wizard.py").read_text(encoding="utf-8")
        start = source.index("subscription = (payload or {}).get")
        block = source[start:start + 900]
        for field in ("quota_micro_usd", "used_micro_usd", "reserved_micro_usd"):
            self.assertIn(field, block)

    def test_the_gateway_sends_them(self):
        source = (ROOT / "src" / "pyweixin" / "product.py").read_text(encoding="utf-8")
        start = source.index("def _subscription_view")
        block = source[start:start + 900]
        for field in ("quota_micro_usd", "used_micro_usd", "reserved_micro_usd"):
            self.assertIn(field, block)

    def test_the_bar_only_appears_when_there_is_a_denominator(self):
        """没分母就不画 —— 硬画一条空条等于说「用了很多」。"""
        source = (ROOT / "src" / "pyweixin" / "merchant_ui.py").read_text(encoding="utf-8")
        self.assertIn("def _render_quota_bar", source)
        start = source.index("def _render_quota_bar")
        block = source[start:start + 1600]
        self.assertIn("unlimited", block)
        self.assertIn("pack_forget", block)


if __name__ == "__main__":
    unittest.main()
