"""微信收进托盘时，界面必须说清楚。

## 现象

商户一边在微信里跟客户聊天，一边跑接单助手，**把微信窗口收进任务栏托盘** ——
这是再正常不过的用法。实测：托盘里留着 Qt 辅助窗口（类 Qt51514QWindowIcon，
标题 WeChat），主窗口 `mmui::MainWindow` 确实不在，所以读不到群消息。

## 缺陷

探测**是对的**（确实读不到），错的是文案：「微信进程在，但既没有登录窗口也
没有主窗口」。

商户一看就知道自己没关微信，然后不知道该做什么 —— 他会以为软件坏了、
会以为要重新登录、会去翻设置。真正的原因和该做的事，一个字都没说。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin.wechat_state import WeChatState  # noqa: E402


class TrayMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parent.parent
                      / "src" / "pyweixin" / "wechat_state.py").read_text(encoding="utf-8")

    def test_it_says_wechat_is_open_rather_than_broken(self):
        """「进程在但找不到窗口」不等于「微信坏了」。"""
        self.assertIn("微信开着，但窗口收起来了", self.source)

    def test_it_tells_the_merchant_what_to_do(self):
        """说了现状还不够，得说下一步。"""
        self.assertIn("把窗口打开", self.source)
        self.assertIn("重新检测", self.source)

    def test_the_old_opaque_wording_is_gone(self):
        """「既没有登录窗口也没有主窗口」对商户没有任何意义。"""
        self.assertNotIn("既没有登录窗口也没有主窗口", self.source)

    def test_it_names_the_tray_where_it_actually_is(self):
        """托盘在右下角。不说位置，商户会满屏找。"""
        self.assertIn("右下角", self.source)

    def test_it_explains_why_reading_will_not_work(self):
        self.assertIn("读不到群消息", self.source)

    def test_it_does_not_claim_wechat_needs_relogin(self):
        """商户没登出任何东西。让他重新登录只会更困惑。"""
        self.assertNotIn("重新登录微信", self.source)


class TrayDetectionTests(unittest.TestCase):
    """不可见的窗口也要算存在 —— 托盘里的窗口仍可被 UIA 找到。"""

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parent.parent
                      / "src" / "pyweixin" / "wechat_state.py").read_text(encoding="utf-8")

    def test_invisible_windows_still_count_as_found(self):
        self.assertIn("visible_only=False", self.source,
                      "只查可见窗口会把「收进托盘」当成「没打开」")

    def test_a_hidden_main_window_is_reported_as_logged_in(self):
        """找到但不可见 → 登录状态是好的，只是读不到内容。

        刻意不在源码里找缩进敏感的片段：格式一改断言就红，
        而它要盯的是**状态**对不对，不是排版。
        """
        self.assertIn("if not main.is_visible()", self.source)
        tail = self.source[self.source.index("if not main.is_visible()"):]
        tail = tail[:tail.index("OFFLINE_TITLES")]
        self.assertIn("WeChatState.LOGGED_IN", tail)


class NoTechnicalLeakTests(unittest.TestCase):
    def test_the_message_names_no_technical_stuff(self):
        source = (Path(__file__).resolve().parent.parent
                  / "src" / "pyweixin" / "wechat_state.py").read_text(encoding="utf-8")
        start = source.index("微信开着，但窗口收起来了")
        message = source[start:start + 160]
        for term in ("mmui", "Qt", "UIA", "pywinauto", "class_name", "进程"):
            self.assertNotIn(term, message.split('"')[1] if '"' in message else message,
                             f"商户可见文案里出现了 {term}")


class RestartAfterUpdateTests(unittest.TestCase):
    """微信更新后卡死（进程在、界面出不来）要有路可走。

    2026-10-13 实录：4.1.13.65 升 4.1.15.55，5 个新进程跑着，
    mmui 窗口一个没有，点托盘也没用，只能杀掉重开。
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.ui = (root / "src" / "pyweixin" / "merchant_ui.py").read_text(encoding="utf-8")
        cls.tools = (root / "src" / "pyweixin" / "WeChatTools.py").read_text(encoding="utf-8")

    def test_restart_kills_before_relaunch(self):
        start = self.tools.index("def restart_weixin")
        block = self.tools[start:start + 1200]
        self.assertIn("process.kill()", block, "不杀旧进程就重开，两个微信会打架")
        self.assertIn("os.startfile", block)

    def test_restart_button_only_shows_when_unreadable(self):
        self.assertIn("def _refresh_wechat_restart", self.ui)
        self.assertIn("WeChatState.UNREADABLE", self.ui)

    def test_restart_asks_first(self):
        start = self.ui.index("def _on_restart_wechat")
        block = self.ui[start:start + 800]
        self.assertIn("askyesno", block, "杀微信不先问，正在输入的内容就没了")

    def test_version_change_is_recorded(self):
        from pyweixin.runmode import MerchantSettings

        restored = MerchantSettings.from_dict(
            MerchantSettings(wechat_version="4.1.15.55").to_dict())
        self.assertEqual(restored.wechat_version, "4.1.15.55")


if __name__ == "__main__":
    unittest.main()