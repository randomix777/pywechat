"""本地把「客户说了什么」匹配成 sku。

## 为什么模型不直接给 sku

群里客户**不报 SKU**，只说「李记酱油」「上次那种」「那种大瓶的」。契约要求
模型把原话放在 ``said``、sku **留 null** —— 因为猜错的代价比留空大得多：
猜错了商户会照着发错货，而留空至少草稿还在、商户能在确认卡上补。

所以 sku 必须由本地按商户目录匹配后填上。这是那半条链路。

## 最重要的一条：匹配不到多个时**不猜**

猜一个就可能发错货。留空让商户选，只多点一下而已。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.orders import _match_items_to_products  # noqa: E402

CATALOG = [
    {"sku": "P-SOY", "name": "酱油", "unit": "瓶", "aliases": "酱油|李记酱油"},
    {"sku": "P-APL", "name": "红富士苹果", "unit": "箱", "aliases": "红富士苹果|苹果"},
]


def item(said, *, brand=None, category=None, sku=None):
    return {"said": said, "brand": brand, "category": category, "sku": sku}


class MatchesByWhatTheCustomerSaidTests(unittest.TestCase):
    def test_it_matches_a_brand_plus_category_phrase(self):
        got = _match_items_to_products([item("李记酱油 2 瓶", brand="李记", category="酱油")], CATALOG)
        self.assertEqual(got, ["P-SOY"])

    def test_it_matches_on_category_when_said_is_too_loose(self):
        """「上次那种」本身匹配不上，得靠拆出来的品类。"""
        got = _match_items_to_products([item("上次那种", category="酱油")], CATALOG)
        self.assertEqual(got, ["P-SOY"])

    def test_a_substring_alias_still_matches(self):
        got = _match_items_to_products([item("3 箱红富士苹果", category="苹果")], CATALOG)
        self.assertEqual(got, ["P-APL"])

    def test_several_items_are_matched_independently(self):
        items = [item("李记酱油 2 瓶", category="酱油"), item("那个上次买的")]
        got = _match_items_to_products(items, CATALOG)
        self.assertEqual(got, ["P-SOY", ""])


class DoesNotGuessTests(unittest.TestCase):
    """这一组是重点。"""

    def test_an_unmatched_phrase_leaves_sku_empty(self):
        got = _match_items_to_products([item("那个上次买的")], CATALOG)
        self.assertEqual(got, [""], "匹配不上必须留空，交给商户补")

    def test_an_empty_phrase_leaves_sku_empty(self):
        self.assertEqual(_match_items_to_products([item("")], CATALOG), [""])

    def test_two_candidates_are_never_narrowed_down_by_guess(self):
        """目录里有两个都叫「苹果」时，**不**挑一个。

        挑一个就有一半概率发错货。留空让商户选，只多点一下。
        """
        ambiguous = [
            {"sku": "P-A", "name": "苹果", "unit": "箱", "aliases": "苹果"},
            {"sku": "P-B", "name": "苹果", "unit": "筐", "aliases": "苹果"},
        ]
        self.assertEqual(_match_items_to_products([item("要苹果")], ambiguous), [""])

    def test_an_existing_sku_is_never_overwritten(self):
        """契约要求模型留 null，但别的来源可能已经填好了。"""
        self.assertEqual(
            _match_items_to_products([item("李记酱油", category="酱油", sku="PRESET")], CATALOG),
            ["PRESET"],
        )


class RobustnessTests(unittest.TestCase):
    def test_a_product_without_aliases_still_matches_by_name(self):
        catalog = [{"sku": "P-X", "name": "土豆", "unit": "袋", "aliases": ""}]
        self.assertEqual(_match_items_to_products([item("土豆 3 袋")], catalog), ["P-X"])

    def test_an_empty_catalog_matches_nothing(self):
        self.assertEqual(_match_items_to_products([item("酱油")], []), [""])

    def test_no_items_is_not_an_error(self):
        self.assertEqual(_match_items_to_products([], CATALOG), [])


if __name__ == "__main__":
    unittest.main()