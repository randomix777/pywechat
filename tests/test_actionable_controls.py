"""点了要有反应 —— 亮着的按钮必须能按，出错必须看得见。

## 两个真实缺陷

### 1 未登录时「开始智能整理」照样亮着

商户没登录就去点整理 —— 必然失败。按钮亮着、点了没反应、也没有任何解释。
这是最糟的一种摩擦：他会以为程序坏了，或者以为自己按错了地方。

### 2 失败消息在第 4 步无处可放

`application.analyze()` 把失败交给 `_error`，而 `_on_error` 只往
``read_state`` 里写 —— **那是第 3 步的控件**。第 4 步的 `log` 也不存在。
两条路都走不通，消息就这么消失了。

商户点「开始智能整理」、失败了、屏幕上什么也没发生。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class ErrorMustBeVisibleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")

    def test_the_analyze_page_has_a_place_to_show_failures(self):
        self.assertIn("analyze_notice", self.source,
                      "第 4 步没有提示位，整理失败的消息就会凭空消失")

    def test_errors_land_wherever_the_user_is(self):
        """不能只往某一个控件写 —— 那个控件不在当前页就等于没写。"""
        start = self.source.index("def _on_error")
        block = self.source[start:start + 1200]
        # 第 7 步分析结果页新增了 analysis_result_summary 落点，
        # 加进来后测试的元组也要跟上（行为本身是对的：多一处落点）。
        self.assertIn('for key in ("read_state", "analysis_result_summary", "analyze_notice"',
                      block, "错误必须按当前屏挑一个存在的落点")

    def test_when_there_is_nowhere_it_still_tells_the_user(self):
        """实在没有落点时也要弹窗。静默永远比打扰更糟。"""
        start = self.source.index("def _on_error")
        block = self.source[start:start + 1600]
        self.assertIn("showwarning", block,
                      "没有落点时应当弹窗，而不是什么都不做")

    def test_a_missing_log_widget_does_not_swallow_the_error(self):
        start = self.source.index("def _on_error")
        block = self.source[start:self.source.index("def _on_log")]
        self.assertIn("except Exception", block,
                      "log 控件不在当前页，不该把错误跟着一起丢掉")


class ButtonHonestyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")

    def test_the_analyze_button_reflects_whether_it_can_be_used(self):
        self.assertIn("def _refresh_analyze_button", self.source)
        start = self.source.index("def _refresh_analyze_button")
        block = self.source[start:start + 1400]
        self.assertIn('state="disabled"', block)

    def test_it_is_refreshed_when_the_page_is_built(self):
        self.assertIn("self._refresh_analyze_button()", self.source)

    def test_it_is_refreshed_after_login(self):
        """登录成功后按钮要立刻可点。灰着的按钮会让人以为程序坏了。"""
        start = self.source.index("def _on_quota")
        block = self.source[start:start + 700]
        self.assertIn("_refresh_analyze_button()", block)

    def test_it_says_why_instead_of_just_going_grey(self):
        start = self.source.index("def _refresh_analyze_button")
        block = self.source[start:start + 1400]
        self.assertIn("点此登录", block,
                      "灰掉必须说明为什么、该去哪里 —— 否则等于让人猜")


class ClickCountTests(unittest.TestCase):
    """少点击是目标之一，那就得有个数。"""

    def test_the_first_run_measurement_script_exists(self):
        script = ROOT / "examples" / "measure_first_run_clicks.py"
        self.assertTrue(script.is_file(),
                        "没有量过点击次数，「简洁」就只是感觉")

    def test_it_reports_the_click_count(self):
        script = (ROOT / "examples" / "measure_first_run_clicks.py").read_text(encoding="utf-8")
        self.assertIn("点了", script)
        self.assertIn("clicks", script)


if __name__ == "__main__":
    unittest.main()