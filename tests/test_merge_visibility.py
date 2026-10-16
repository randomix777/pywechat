"""短消息合并必须让商户看得见。

商户发「苹果两箱」「再加一箱」「明天送」三条，系统合成一条去整理。屏幕上
只出现一张订单 —— 他不知道是合并过的，就可能以为漏单，然后**重发一遍**。
那就不是"少一次点击"，是重复下单。

## 一条曾经永远走不到的分支

首页原本想显示「合并中的短消息组 N 组」，写法是::

    if hasattr(view, "open_merge_groups"):

而 ``HomeView`` **从来没有这个字段**。分支永远为假，商户永远看不到。

用 hasattr 藏起来不会让问题消失，只会让它永远看不见 —— 这条测试就是
为了让"藏起来"这件事没法再悄悄发生。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "order_agent" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "business_agent" / "src"))

from wechat_ai_assistant.dashboard import HomeView  # noqa: E402


class MergeVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parent.parent
                       / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")
        self.code = "\n".join(line.split("#")[0] for line in self.source.splitlines())

    def test_the_merge_count_is_not_guarded_by_hasattr(self):
        """hasattr 会让"字段根本不存在"这件事静默通过。"""
        self.assertNotIn("hasattr(view, \"open_merge_groups\")", self.code)
        self.assertNotIn("hasattr(view, 'open_merge_groups')", self.code)

    def test_the_view_really_has_the_field(self):
        self.assertTrue(hasattr(HomeView(), "open_merge_groups"))

    def test_it_defaults_to_zero_rather_than_guessing(self):
        self.assertEqual(HomeView().open_merge_groups, 0)

    def test_merged_counts_are_reported_to_the_interface(self):
        self.assertIn("on_merged", self.source)

    def test_the_merge_line_is_on_screen(self):
        self.assertIn("listen_merged", self.code)
        self.assertIn("合成一条整理", self.code)


class MergeCountWordingTests(unittest.TestCase):
    """商户读到的那句话要回答他的疑问：为什么三条变一条。"""

    def setUp(self):
        self.source = (Path(__file__).resolve().parent.parent
                       / "src" / "wechat_ai_assistant" / "merchant_ui.py").read_text(encoding="utf-8")

    def test_it_says_the_content_was_not_lost(self):
        """商户的第一反应是"漏单了"。这句话必须先回答这个。"""
        self.assertIn("内容没丢", self.source)

    def test_it_mentions_waiting_a_little(self):
        self.assertIn("先攒一会儿", self.source)


class MergeSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parent.parent
                       / "src" / "wechat_ai_assistant" / "service.py").read_text(encoding="utf-8")

    def test_the_service_reports_which_messages_were_merged(self):
        self.assertIn("on_merged", self.source)
        self.assertIn("merged_event_ids", self.source)

    def test_a_broken_listener_cannot_break_order_processing(self):
        """界面挂了不该让订单处理跟着炸。"""
        code = self.source
        start = code.index("if self.on_merged is not None:")
        block = code[start:start + 200]
        self.assertIn("except Exception", block)


if __name__ == "__main__":
    unittest.main()