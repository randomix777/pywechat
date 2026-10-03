"""微信状态检测的回归测试。**不需要真的装微信**。

上一版有个永远不会自我纠正的缺陷：登录窗口那一侧写成了
``Login_window_Control.LoginWindow``（在**类**上取一个只在 ``__init__`` 里
赋值的实例属性），必然抛 ``AttributeError``；那个异常被 ``except Exception``
吞掉之后直接 ``return WAITING_SCAN``。于是不管微信是什么状态，界面永远显示
"请扫码"。

主窗口那一侧也不对：``Windows.Main_window.MainWindow`` 里 ``Windows`` 是
``Window_Control`` 实例，压根没有 ``Main_window`` 这个属性。

这两条在这里用注入的假 Desktop 钉死：**查找失败必须落到"读不出来"，
绝不能变成"等待扫码"或"已登录"。**
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from pyweixin import wechat_state  # noqa: E402
from pyweixin.wechat_state import (  # noqa: E402
    OFFLINE_TITLES,
    RealWeChatProbe,
    WeChatState,
    WeChatStatus,
)


class _FakeChild:
    def __init__(self, exists: bool = False):
        self._exists = exists

    def exists(self, timeout: float = 0.0) -> bool:
        return self._exists


class _FakeWindow:
    def __init__(self, spec: dict, exists: bool, children: set | None = None, raises: bool = False):
        self.spec = spec
        self._exists = exists
        self._children = set(children or ())
        self._raises = raises

    def exists(self, timeout: float = 0.0) -> bool:
        if self._raises:
            raise RuntimeError("UIA 不可用")
        return self._exists

    def child_window(self, **kwargs):
        key = (kwargs.get("title"), kwargs.get("control_type"))
        return _FakeChild(key in self._children)


class _FakeDesktop:
    """按类名回答"窗口在不在"。找不到就当作不存在。"""

    def __init__(self, main: bool = True, login: bool = False, offline: str = "", raises: bool = False):
        self._main, self._login, self._offline, self._raises = main, login, offline, raises
        self.asked: list[dict] = []

    def __call__(self, backend: str = ""):
        del backend
        return self

    def window(self, **spec):
        self.asked.append(spec)
        if self._raises:
            return _FakeWindow(spec, False, raises=True)
        cls = str(spec.get("class_name") or "")
        if "LoginWindow" in cls:
            return _FakeWindow(spec, self._login)
        if "MainWindow" in cls:
            children = {(self._offline, "Button")} if self._offline else set()
            return _FakeWindow(spec, self._main, children=children)
        return _FakeWindow(spec, False)


class LoginWindowDetectionTests(unittest.TestCase):
    def test_no_login_window_means_no_login_state(self):
        """登录窗口不在 ⇒ 返回 None，让主窗口说话。"""
        probe = RealWeChatProbe()
        self.assertIsNone(probe._login_window_state(_FakeDesktop(login=False)))

    def test_lookup_failure_never_becomes_waiting_scan(self):
        """查找本身出错时不能报"等待扫码" —— 那是谎报。"""
        probe = RealWeChatProbe()
        self.assertIsNone(probe._login_window_state(_FakeDesktop(raises=True)))

    def test_class_level_access_would_have_been_the_bug(self):
        """记录当初那个 bug 的形态：类上没有 LoginWindow。"""
        from pyweixin.Uielements import Login_window_Control, Window_Control

        with self.assertRaises(AttributeError):
            _ = Login_window_Control.LoginWindow          # noqa: B018 - 就是要复现
        self.assertNotIn("Main_window", vars(Window_Control()))
        # 正确写法是取实例
        self.assertEqual(Login_window_Control().LoginWindow, {"class_name": "mmui::LoginWindow"})


class MainWindowDetectionTests(unittest.TestCase):
    def test_main_window_is_matched_by_class_not_by_title(self):
        """标题随语言/版本变（微信 / Weixin / WeChat），类名不变。"""
        probe = RealWeChatProbe()
        desktop = _FakeDesktop(main=True, login=False)
        window = probe._find_window(desktop, wechat_state.MAIN_WINDOW_SPECS)
        self.assertIsNotNone(window)
        # 第一条描述符就是语言无关的类名 + 框架
        self.assertEqual(desktop.asked[0].get("class_name"), "mmui::MainWindow")
        self.assertEqual(desktop.asked[0].get("framework_id"), "Qt")
        self.assertNotIn("title", desktop.asked[0])

    def test_a_wechat_titled_window_is_still_recognised(self):
        """本机 4.x 的主窗口 UIA 标题是 ``Weixin``，Win32 类名是 ``Qt51514QWindowIcon``；
        UIA 里则是 ``mmui::MainWindow``。标题只作兜底，不作主判据。"""
        self.assertIn({"class_name": "mmui::MainWindow", "title": "WeChat"}, wechat_state.MAIN_WINDOW_SPECS)
        self.assertIn({"class_name": "mmui::MainWindow", "title": "Weixin"}, wechat_state.MAIN_WINDOW_SPECS)
        self.assertIn({"class_name": "mmui::MainWindow", "title": "微信"}, wechat_state.MAIN_WINDOW_SPECS)

    def test_offline_titles_cover_every_known_language(self):
        self.assertEqual(len(OFFLINE_TITLES), 3)
        self.assertIn("当前网络不可用", OFFLINE_TITLES)
        self.assertIn("Network unavailable", OFFLINE_TITLES)


class ProbeDecisionTests(unittest.TestCase):
    """把 :meth:`RealWeChatProbe.probe` 的判定分支单独跑一遍。"""

    def _probe(self, monkey_state, **kwargs):
        probe = RealWeChatProbe(timeout=0.0, **kwargs)
        # 绕开"微信装没装/进程在不在"，只测窗口判定这一段。
        from pyweixin.WeChatTools import Tools

        original_where, original_running = Tools.where_weixin, Tools.is_weixin_running
        Tools.where_weixin = lambda: "C:/fake/Weixin.exe"
        Tools.is_weixin_running = lambda: True
        desktop = _FakeDesktop(**monkey_state)
        try:
            import pyweixin.wechat_state as module

            real_desktop = module.pywinauto_desktop = None  # 标记占位
            del real_desktop
            original_import = module.Desktop if hasattr(module, "Desktop") else None
            del original_import
            # 直接注入假 Desktop
            import pywinauto

            original_desktop_class = pywinauto.Desktop
            pywinauto.Desktop = desktop
            try:
                return probe.probe()
            finally:
                pywinauto.Desktop = original_desktop_class
        finally:
            Tools.where_weixin, Tools.is_weixin_running = original_where, original_running

    def test_logged_in_when_only_the_main_window_is_there(self):
        status = self._probe({"main": True, "login": False})
        self.assertEqual(status.state, WeChatState.LOGGED_IN)
        self.assertIn("已登录", status.copy()[0])

    def test_offline_button_wins_over_logged_in(self):
        status = self._probe({"main": True, "login": False, "offline": "Network unavailable"})
        self.assertEqual(status.state, WeChatState.OFFLINE)

    def test_login_window_reports_waiting_scan(self):
        status = self._probe({"main": True, "login": True})
        self.assertEqual(status.state, WeChatState.WAITING_SCAN)

    def test_neither_window_is_unreadable_not_waiting_scan(self):
        """进程在但两个窗口都找不到 ⇒ 读不出来。绝不能报"等待扫码"。"""
        status = self._probe({"main": False, "login": False})
        self.assertEqual(status.state, WeChatState.UNREADABLE)
        self.assertNotEqual(status.state, WeChatState.WAITING_SCAN)

    def test_lookup_failure_is_unreadable_not_waiting_scan(self):
        status = self._probe({"raises": True})
        self.assertEqual(status.state, WeChatState.UNREADABLE)
        self.assertNotEqual(status.state, WeChatState.WAITING_SCAN)


class StatusIsAlwaysAnnouncedTests(unittest.TestCase):
    """关掉微信再点「重新检测」，界面必须跟着变。

    以前 ``Wizard.check_wechat`` 只在**已就绪**时才发事件，于是状态其实变了、
    界面却一动不动 —— 商户看到的是上一轮的"已登录"。这里用注入的监视器把
    三种往返都跑一遍。
    """

    def _wizard(self, states):
        from pyweixin.wizard import Wizard

        class _Monitor:
            def __init__(self):
                self.latest = None
                self._states = list(states)

            def check_once(self):
                state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
                self.latest = WeChatStatus(state, "详情", account_id="wxid_x", checked_at=1.0)
                return self.latest

        class _Store:
            def __init__(self):
                self.settings = None

            def load(self):
                class _S:
                    account_id = "wxid_x"
                    wizard_complete = True

                    def reset_for_new_account(self):
                        pass
                return _S()

            def save(self, _settings):
                pass

        seen: list[tuple[str, list[str]]] = []
        steps: list[str] = []
        monitor = _Monitor()
        wizard = Wizard(
            _Store(), monitor, None, None, tenant="t",
            on_step=steps.append,
            on_wechat=lambda status: seen.append((status.state, list(steps))),
        )
        return wizard, seen, steps

    def test_ready_then_closed_announces_both_and_falls_back_to_step_one(self):
        wizard, seen, steps = self._wizard(["logged_in", "not_started"])
        wizard.check_wechat()
        self.assertEqual(seen[-1][0], "logged_in")
        self.assertEqual(steps[-1], "conversations")

        wizard.check_wechat()
        self.assertEqual(seen[-1][0], "not_started", "关掉微信后必须把新状态报出来")
        self.assertEqual(steps[-1], "wechat", "读不到微信时必须退回第 1 步")

    def test_every_check_announces_not_only_the_ready_ones(self):
        wizard, seen, _ = self._wizard(["not_started", "waiting_scan"])
        wizard.check_wechat()
        wizard.check_wechat()
        self.assertEqual([state for state, _ in seen], ["not_started", "waiting_scan"])

    def test_repeated_not_ready_checks_do_not_navigate(self):
        wizard, _, steps = self._wizard(["waiting_scan"])
        wizard.check_wechat()
        wizard.check_wechat()
        self.assertNotIn("wechat", steps[1:], "已经在第 1 步时不要反复跳转")

    def test_app_exposes_the_event(self):
        """事件必须真的挂上，否则界面永远收不到通知。"""
        from pyweixin.application import AppEvents

        self.assertIn("on_wechat", AppEvents.__dataclass_fields__)
        events = AppEvents()
        self.assertIsNone(events.on_wechat, "默认不绑界面时也不该报错")


class LanguageIsNotFrozenTests(unittest.TestCase):
    """启动时微信没开，界面语言不能就此定格在默认值上。

    ``Uielements`` 的会话列表、消息区、搜索框描述符是**导入时**按当时的语言
    建好的。语言检测失败时若把默认值缓存住，之后微信打开也不会重新检测，
    读会话就会"找不到列表"。
    """

    def _config(self, detector):
        from pyweixin.Config import GlobalConfig

        original_detector, original_value = GlobalConfig._language_detector, GlobalConfig._language
        GlobalConfig._language_detector = detector
        GlobalConfig._language = None
        return GlobalConfig, lambda: (
            setattr(GlobalConfig, "_language_detector", original_detector),
            setattr(GlobalConfig, "_language", original_value),
        )

    def test_failed_detection_returns_a_usable_value_but_is_not_cached(self):
        config, restore = self._config(lambda: None)
        try:
            self.assertEqual(config.language, "简体中文")     # 调用方拿得到字符串
            self.assertFalse(config.language_detected)        # 但知道这是猜的
            self.assertIsNone(config._language)              # 缓存里不留猜测
        finally:
            restore()

    def test_language_is_retried_once_wechat_is_up(self):
        answers = [None, "English"]
        config, restore = self._config(lambda: answers.pop(0) if answers else "English")
        try:
            self.assertEqual(config.language, "简体中文")
            self.assertFalse(config.language_detected)
            # 微信这时打开了
            self.assertEqual(config.refresh_language(), "English")
            self.assertEqual(config.language, "English")
            self.assertTrue(config.language_detected)
        finally:
            restore()

    def test_refreshing_to_the_same_language_reports_no_change(self):
        from pyweixin.Config import GlobalConfig

        config, restore = self._config(lambda: "English")
        try:
            GlobalConfig._language = "English"
            self.assertIsNone(config.refresh_language())
            self.assertEqual(config.language, "English")
        finally:
            restore()

    def test_selectors_are_rebuilt_when_the_language_changes(self):
        """重建之后，语言专属的描述符必须真的跟着换。"""
        from pyweixin import Uielements
        from pyweixin.wechat_state import RealWeChatProbe

        original = Uielements.Main_window
        try:
            Uielements.Main_window = Uielements.Main_window_Control(language="简体中文", Version="4.1.0")
            chinese = Uielements.Main_window.MainWindow["title"]
            rebuilt = RealWeChatProbe()._refresh_ui_language()
            if rebuilt:
                self.assertNotEqual(Uielements.Main_window.MainWindow["title"], chinese)
            else:
                self.skipTest("本机语言与默认一致，无需重建")
        finally:
            Uielements.Main_window = original


if __name__ == "__main__":
    unittest.main()
