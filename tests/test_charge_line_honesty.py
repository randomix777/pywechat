"""扣费文案不能对商户撒谎。

## 背景

计价改成美元之后，不再有「每条消息扣 N 次额度」这个固定数字了 ——
真实成本随消息长度变化。所以文案不能说死一个数。

但**原则没变**：展示文案是给商户看的，一段中文不能悄悄改掉扣费依据。
更不能反过来 —— **不能声称免费**。免费档位已经焊死（产品规则：免费模型
也扣分析额度），收费模型自称免费一定是在骗商户。

这两类谎话以前只抓得住「扣 N 次」那一种，「免费」根本检测不到。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.model_catalog import _claims_to_be_free, _note_contradicts  # noqa: E402


class FreeClaimsTests(unittest.TestCase):
    def test_claiming_free_is_caught(self):
        """收费模型自称免费 = 骗商户，必须被拦住。"""
        for lie in (
            "免费使用，不消耗额度",
            "这个模型不收费",
            "无需付费",
            "不用付费",
            "不扣额度",
        ):
            with self.subTest(lie=lie):
                self.assertTrue(_claims_to_be_free(lie), f"没拦住这句谎话：{lie}")

    def test_a_negative_statement_is_not_a_lie(self):
        """「不是免费模型，但会扣费」是在**如实说明**，不该被丢掉。

        丢掉它反而是错的：那会让运营没法向商户解释清楚。
        """
        for honest in (
            "不是免费模型，分析会消耗额度",
            "并非免费，但会计费",
            "不会免费提供",
        ):
            with self.subTest(honest=honest):
                self.assertFalse(_claims_to_be_free(honest), f"误伤了如实说明：{honest}")

    def test_ordinary_wording_is_left_alone(self):
        for text in ("分析较慢，会占用额度", "该模型会消耗分析额度", ""):
            with self.subTest(text=text):
                self.assertFalse(_claims_to_be_free(text))


class ContradictionTests(unittest.TestCase):
    def test_a_wrong_count_is_still_caught(self):
        """改成美元计价不意味着放弃这条：次数与策略不符仍以策略为准。"""
        self.assertTrue(_note_contradicts("每分析一条消息扣 1 次额度", 3))
        self.assertFalse(_note_contradicts("每分析一条消息扣 3 次额度", 3))

    def test_descriptive_text_is_not_thrown_away(self):
        self.assertFalse(_note_contradicts("分析较慢，会占用额度", 2))


class MerchantFacingLineTests(unittest.TestCase):
    def test_the_line_does_not_promise_a_fixed_number(self):
        """美元计价下不存在「固定扣 N 次」这回事，说死一个数就是撒谎。"""
        import re

        from wechat_ai_assistant.model_catalog import PricingClass, build_catalog, CatalogStore, OperatorModelPolicy

        policy = OperatorModelPolicy(
            alias="m", provider_id="p", model_id="m",
            pricing_class=PricingClass.METERED, cost_micro_usd_per_job=1,
        )
        store = CatalogStore()
        store.load_policies([policy.to_dict()])
        store.catalog = build_catalog(
            [{"provider_id": "p", "name": "p", "connected": True,
              "models": [{"model_id": "m", "display_name": "深", "context_limit": 1,
                          "output_limit": 1, "capabilities": {}, "upstream_cost": {},
                          "upstream_status": "live"}]}],
            {"m": policy},
        )
        line = store.catalog.entry("m").charge_line()
        self.assertNotRegex(line, re.compile(r"扣\s*\d+\s*(次|个|笔)"),
                            "文案仍在承诺一个固定的扣费次数，美元计价下那是假的")
        self.assertIn("实际长度", line, "必须说清成本取决于消息长度")


if __name__ == "__main__":
    unittest.main()