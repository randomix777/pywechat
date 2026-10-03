"""收尾体检脚本本身必须诚实。

`examples/closing_health_check.py` 会输出一份「做完了什么、没做什么」的清单。
这份清单最大的风险是：**它自己变成一份会过期的自我表扬**。

所以盯住三件事：

1. 已知未修的问题**必须**列出来，不许悄悄消失
2. 数字**必须现跑**，不许写死（写死的数字必然过期）
3. 它列出来的「已完成」不许把未完成项混进去
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

import closing_health_check as health  # noqa: E402


class KnownIssuesMustStayListedTests(unittest.TestCase):
    """未修好的问题不许从清单里消失。

    「读取 0 条」这个坑查了五轮都没查明。任何把它写成"已修复"的改动，
    都应当让这里变红 —— 除非真有一天拿到了证据。
    """

    def test_the_unresolved_read_issue_is_still_listed(self):
        titles = [title for title, _why, _note in health.KNOWN_ISSUES]
        self.assertTrue(
            any("0 条" in title or "返回 0" in title for title in titles),
            "「读取历史偶尔返回 0 条」从未查明，不能从清单里消失",
        )

    def test_it_says_it_is_unresolved(self):
        for title, why, _note in health.KNOWN_ISSUES:
            if "0 条" in title or "返回 0" in title:
                self.assertIn("未查明", why,
                              "必须写明是未查明，不能含糊成'偶发'")

    def test_it_does_not_claim_the_lock_fix_solved_it(self):
        """那个改动是对的，但它**不是**这个故障的原因。"""
        for title, _why, note in health.KNOWN_ISSUES:
            if "0 条" in title or "返回 0" in title:
                self.assertIn("没有证据", note,
                              "不得把无关联的修复说成根因已找到")

    def test_real_sending_is_still_marked_unverified(self):
        titles = [title for title, _why, _note in health.KNOWN_ISSUES]
        self.assertTrue(any("真机" in title for title in titles),
                        "真实发送从未验证过，不能悄悄算作完成")


class NoHardcodedNumbersTests(unittest.TestCase):
    """体检脚本不能自己犯「写死数字必然过期」这个毛病。"""

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "examples" / "closing_health_check.py").read_text(
            encoding="utf-8")

    def test_the_test_count_is_computed_not_written(self):
        self.assertIn("def count_tests", self.source,
                      "测试数量必须现跑")
        self.assertIn("Ran (\\d+) tests", self.source)

    def test_no_bare_number_is_claimed_in_the_summary(self):
        import re

        body = self.source[ self.source.index("已完成的部分") : ] \
            if "已完成的部分" in self.source else ""
        # 允许 f-string 里的 {total}，不允许 "846 项" 这种写死的。
        self.assertNotRegex(body, r"\d{3,}\s*项",
                            "收尾体检里出现了写死的数字")

    def test_it_says_where_the_numbers_come_from(self):
        self.assertIn("不是抄的", self.source)


class UnverifiedListTests(unittest.TestCase):
    """没验证过的能力必须一直列着。"""

    def test_deepseek_is_still_unverified(self):
        titles = [title for title, _why in health.UNVERIFIED]
        self.assertTrue(any("DeepSeek" in title for title in titles))

    def test_jyi_is_still_blocked(self):
        titles = [title for title, _why in health.UNVERIFIED]
        self.assertTrue(any("JYI" in title for title in titles))

    def test_there_are_decisions_waiting_on_the_user(self):
        """这些不是缺陷，是取舍 —— 得让人知道它们还在。"""
        self.assertGreaterEqual(len(health.BLOCKED_ON_USER), 3)


if __name__ == "__main__":
    unittest.main()