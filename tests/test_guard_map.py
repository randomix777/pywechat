"""护栏对照表本身可不可信。

`examples/guard_map.py` 报「19 条症状全部已护住」——这种结果最需要怀疑：
一个只会说"都很好"的工具，和没有工具一样没用。

这里证明三件事：

1. 它**能**报出没护住的症状（用一个不存在的断言名试）
2. 它的匹配**不会**被无关代码误触发（曾经把"不再挂在待发送回执后面"
   当成"后台任务失败"的证据）
3. 每条症状都必须真的对应一条断言，不能只是登记在册
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from guard_map import GUARDS, scan  # noqa: E402


class DetectorTests(unittest.TestCase):
    def test_it_reports_a_symptom_with_no_assertion(self):
        """工具必须能说"没护住"。只会说好话的工具没有价值。"""
        fake = ("假装", "这是一个还没修的症状", ("这句话不会出现在任何断言里_xyz",))
        hits = scan()
        found = any(hits.get(needle) for needle in fake[2])
        self.assertFalse(found, "用一个不存在的断言名都能匹配上，匹配逻辑坏了")

    def test_a_real_guard_is_found(self):
        """反过来也成立：真实存在的断言要能被找到。"""
        hits = scan()
        needle = "test_the_analyze_button_reflects_whether_it_can_be_used"
        self.assertTrue(hits.get(needle),
                        "真实存在的断言却没被找到，匹配逻辑也有问题")

    def test_unrelated_lines_do_not_count_as_evidence(self):
        """曾经用 `不再挂` 当关键字，把「来源说明不再挂在待发送回执后面」
        当成「后台任务失败」的证据 —— 一条完全无关的断言。

        现在关键字必须是断言名或完整短语，撞车概率小得多。
        """
        needles = [n for _c, _d, ns in GUARDS for n in ns]
        for needle in needles:
            self.assertGreaterEqual(len(needle), 6,
                                    f"关键字太短，容易误匹配：{needle!r}")

    def test_every_symptom_has_at_least_one_needle(self):
        for category, symptom, needles in GUARDS:
            with self.subTest(symptom=symptom):
                self.assertTrue(needles, f"{category}／{symptom} 没有任何检查关键字")


class CoverageReportTests(unittest.TestCase):
    """这一组是真正在跑的：它要求每条登记在册的症状都有断言护着。"""

    def test_no_symptom_is_left_unguarded(self):
        hits = scan()
        missing = [
            f"{category}／{symptom}"
            for category, symptom, needles in GUARDS
            if not any(hits.get(needle) for needle in needles)
        ]
        self.assertFalse(
            missing,
            "这些商户看得见的症状还没有断言护着：\n  " + "\n  ".join(missing),
        )


class MapKeepsUpTests(unittest.TestCase):
    """对照表要跟着代码一起变，不能停在某一轮的状态。"""

    def test_the_script_exists_and_is_executable(self):
        script = ROOT / "examples" / "guard_map.py"
        self.assertTrue(script.is_file())
        self.assertIn("if __name__", script.read_text(encoding="utf-8"))

    def test_it_scans_both_unit_tests_and_the_ui_check(self):
        script = (ROOT / "examples" / "guard_map.py").read_text(encoding="utf-8")
        self.assertIn('tests").glob("test_*.py")', script)
        self.assertIn("merchant_ui_check.py", script,
                      "只扫单测会漏掉真实窗口上的那些断言")


if __name__ == "__main__":
    unittest.main()