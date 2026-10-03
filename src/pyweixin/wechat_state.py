"""微信状态检测：不抢焦点、可重复、结果可解释。

商户第一次双击软件时，软件必须先回答一个问题：**现在能不能干活？** 这个问题
只有一个正确答案才算数，所以检测必须区分下面七种状态，而不是"进程在就当登录了"：

===========================  ==========================================
状态                         含义与界面动作
===========================  ==========================================
``not_installed``            没装 PC 微信 —— 提示去下载，没有按钮
``not_started``              装了但没开 —— 显示"打开微信"
``waiting_scan``            开着但停在扫码页 —— 提示去官方窗口扫码
``waiting_confirm``         扫码后等手机确认 —— 提示去手机点确认
``logged_in``               已登录 —— 直接进入下一步
``offline``                 微信自己显示断网 —— 提示恢复网络
``unreadable``              窗口在但认不出来 —— 提示重启微信
===========================  ==========================================

## 被动检测不得改变前台

这是硬约束。``Navigator.open_weixin()`` 会做 ``SetWindowPos(HWND_TOPMOST)``、
``MoveWindow`` 和最大化——那是有意的「把窗口摆到用户面前」动作，只在**用户点了
「打开微信」**时才允许调用。

被动检测只走 ``win32gui.FindWindow`` 和 UIA 的 ``exists()``，两者都不会置顶、
移动、恢复或激活窗口。否则商户正在打字，每 3 秒被拽走一次焦点。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

LOGGER = logging.getLogger("pyweixin.wechat")

#: 语言无关的窗口身份。
#:
#: 微信主窗口的**标题**随界面语言和版本变（``微信`` / ``Weixin`` / ``WeChat``），
#: 而类名和 UI 框架不会变。以前检测直接用 ``Main_window_Control`` 里那个带标题
#: 的描述符，于是"界面语言 ≠ 配置里的语言"时主窗口**永远匹配不上**；登录窗口
#: 那一侧还写成了 ``Login_window_Control.LoginWindow``（类上取实例属性），
#: 必然抛 ``AttributeError``，被 ``except`` 吞掉之后**恒定**返回"等待扫码"。
#: 结果是：不管微信是什么状态，界面永远显示"请扫码"。
#:
#: 认类名和框架就够了，标题只用来兜底。
MAIN_WINDOW_SPECS: tuple[dict[str, Any], ...] = (
    {"class_name": "mmui::MainWindow", "framework_id": "Qt"},
    {"class_name": "mmui::MainWindow"},
    {"class_name": "mmui::MainWindow", "title": "Weixin"},
    {"class_name": "mmui::MainWindow", "title": "WeChat"},
    {"class_name": "mmui::MainWindow", "title": "微信"},
)
LOGIN_WINDOW_SPECS: tuple[dict[str, Any], ...] = (
    {"class_name": "mmui::LoginWindow"},
    {"class_name": "mmui::LoginWindow", "title": "微信"},
    {"class_name": "mmui::LoginWindow", "title": "Weixin"},
    {"class_name": "mmui::LoginWindow", "title": "WeChat"},
)
#: 断网提示的标题，同样按语言给全。
OFFLINE_TITLES: tuple[str, ...] = ("当前网络不可用", "Network unavailable", "目前網路不可用")


class WeChatState:
    """七种互斥状态。字符串值用于持久化与界面判断。"""

    NOT_INSTALLED = "not_installed"
    NOT_STARTED = "not_started"
    WAITING_SCAN = "waiting_scan"
    WAITING_CONFIRM = "waiting_confirm"
    LOGGED_IN = "logged_in"
    OFFLINE = "offline"
    UNREADABLE = "unreadable"

    ALL = (
        NOT_INSTALLED,
        NOT_STARTED,
        WAITING_SCAN,
        WAITING_CONFIRM,
        LOGGED_IN,
        OFFLINE,
        UNREADABLE,
    )

    #: 只有这些状态允许继续往下走。
    READY = (LOGGED_IN,)


#: 状态 -> (界面主标题, 界面说明, 主按钮文字)
#: 按钮为空表示该状态没有可点的动作。
STATE_COPY: dict[str, tuple[str, str, str]] = {
    WeChatState.NOT_INSTALLED: (
        "未检测到电脑版微信",
        "请先在这台电脑上安装并登录电脑版微信（4.0 及以上），然后重新检测。",
        "",
    ),
    WeChatState.NOT_STARTED: (
        "微信还没有打开",
        "点击下面的按钮打开微信，然后在微信里登录您的账号。",
        "打开微信",
    ),
    WeChatState.WAITING_SCAN: (
        "请在微信窗口扫码",
        "微信已经打开，正在等待扫码。请在微信官方窗口里用手机扫码，不要在本软件里输入密码。",
        "",
    ),
    WeChatState.WAITING_CONFIRM: (
        "请在手机上确认登录",
        "扫码已完成，正在等待手机确认。请在微信手机上点确认登录。",
        "",
    ),
    WeChatState.LOGGED_IN: ("微信已登录", "可以继续选择要接单的群。", ""),
    WeChatState.OFFLINE: (
        "微信显示网络不可用",
        "微信自己提示网络不可用，此时无法读取群消息。请先恢复网络后重新检测。",
        "",
    ),
    WeChatState.UNREADABLE: (
        "暂时无法读取微信界面",
        "微信窗口在，但内容读不出来。请尝试把微信完全退出后重新打开；仍然不行请重启电脑。",
        "",
    ),
}


@dataclass(frozen=True)
class WeChatStatus:
    """一次检测的完整结果，包含证据，便于排查而不是只给一个布尔值。"""

    state: str
    detail: str = ""
    #: 登录态下的账号标识（wxid 目录名）。用于绑定会话与身份。
    account_id: str = ""
    checked_at: float = 0.0
    #: True 表示这次结果来自模拟器，不是真实微信。
    simulated: bool = False

    def __post_init__(self) -> None:
        if self.state not in WeChatState.ALL:
            raise ValueError(f"unknown wechat state: {self.state}")

    @property
    def ready(self) -> bool:
        return self.state in WeChatState.READY

    def copy(self) -> tuple[str, str, str]:
        return STATE_COPY[self.state]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "account_id": self.account_id,
            "checked_at": self.checked_at,
            "simulated": self.simulated,
        }


class WeChatProbe(Protocol):
    """检测微信状态。可注入，因此界面与回环测试都能在没有微信的机器上跑。"""

    def probe(self) -> WeChatStatus:
        """返回当前状态。必须是无副作用的被动检测。"""


class WeChatLauncher(Protocol):
    def launch(self) -> bool:
        """启动微信。返回是否成功。仅在用户明确点击后调用。"""


# --------------------------------------------------------------------- 真实


class RealWeChatProbe:
    """基于 UIA 的真实检测。

    全部使用非激活式查询：``win32gui.FindWindow`` 找句柄，UIA ``exists()``
    判断控件是否出现。两者都不会把微信窗口置顶或抢焦点。
    """

    def __init__(self, clock: Callable[[], float] = time.time, timeout: float = 0.3):
        self._clock = clock
        self._timeout = timeout

    def probe(self) -> WeChatStatus:
        checked_at = self._clock()
        try:
            from .Errors import NotInstalledError
            from .WeChatTools import Tools
        except Exception as exc:  # pragma: no cover - Windows-only import guard
            return WeChatStatus(WeChatState.UNREADABLE, f"无法加载微信控制模块：{exc}", checked_at=checked_at)

        # 1) 安装检查：读注册表，不碰窗口。
        try:
            Tools.where_weixin()
        except NotInstalledError:
            return WeChatStatus(WeChatState.NOT_INSTALLED, "注册表中没有找到电脑版微信", checked_at=checked_at)
        except Exception as exc:
            return WeChatStatus(WeChatState.UNREADABLE, f"检测安装状态失败：{exc}", checked_at=checked_at)

        # 2) 进程检查：进程存在**不等于**登录成功，这里只用来区分未启动。
        try:
            if not Tools.is_weixin_running():
                return WeChatStatus(WeChatState.NOT_STARTED, "微信进程未运行", checked_at=checked_at)
        except Exception as exc:
            return WeChatStatus(WeChatState.UNREADABLE, f"检测进程失败：{exc}", checked_at=checked_at)

        # 3) 窗口检查。登录窗口优先于主窗口。
        try:
            from pywinauto import Desktop
        except Exception as exc:  # pragma: no cover
            return WeChatStatus(WeChatState.UNREADABLE, f"无法加载界面模块：{exc}", checked_at=checked_at)

        login_state = self._login_window_state(Desktop)
        if login_state is not None:
            return WeChatStatus(login_state, "检测到微信登录窗口", checked_at=checked_at)

        main = self._find_window(Desktop, MAIN_WINDOW_SPECS)
        if main is None:
            # 进程在、主窗口不在 —— 十有八九是**商户把微信收进了任务栏托盘**。
            # 这不是"出了故障"，也不该报成一句冷冰冰的"无法读取"：他一看
            # 就知道自己没关微信，然后不知道下一步该做什么。
            #
            # 实测：托盘里留着的是 Qt 的辅助窗口（类 Qt51514QWindowIcon，
            # 标题 WeChat），主窗口 mmui::MainWindow 确实不在。读不到内容，
            # 所以这里只负责**说清楚现状和该做什么**，不去假装能读。
            return WeChatStatus(
                WeChatState.UNREADABLE,
                "微信开着，但窗口收起来了，读不到群消息。"
                "请点一下任务栏右下角的微信图标，把窗口打开，再回来点「重新检测」。",
                checked_at=checked_at,
            )

        # 窗口在、但被收进托盘时读不到内容。**必须说清楚**，否则商户点
        # 「开始读取」之后什么也读不到，只会以为软件坏了。
        try:
            if not main.is_visible():
                return WeChatStatus(
                    WeChatState.LOGGED_IN,
                    "微信已登录，但窗口收在任务栏右下角。"
                    "读消息前请先点一下托盘里的微信把窗口打开。",
                    checked_at=checked_at,
                )
        except Exception as exc:  # pragma: no cover - 取决于本机 UIA
            LOGGER.debug("visibility check failed: %s", exc)

        # 4) 主窗口在，但可能自己显示断网。断网时 UI 自动化读不到东西。
        for title in OFFLINE_TITLES:
            try:
                if main.child_window(title=title, control_type="Button").exists(timeout=0.1):
                    return WeChatStatus(
                        WeChatState.OFFLINE, f"微信主窗口显示「{title}」", checked_at=checked_at
                    )
            except Exception:
                # 认不出断网按钮不等于断网，继续按已登录处理，但要留下证据。
                break

        # 5) 微信从关到开时补一次语言检测。界面文案选择器是导入时按语言建好的，
        #    开始时微信没开就会一直停在默认语言上，读会话会找不到列表。
        self._refresh_ui_language()

        account_id = ""
        try:
            from .WeChatTools import Tools as _Tools

            folder = _Tools.where_wxid_folder()
            account_id = folder.rstrip("\\/").split("\\")[-1].split("/")[-1] if folder else ""
        except Exception:
            # 拿不到 wxid 不影响读取；只是不能做账号切换的强校验。
            account_id = ""
        return WeChatStatus(WeChatState.LOGGED_IN, "检测到已登录的微信主窗口", account_id=account_id, checked_at=checked_at)

    def _refresh_ui_language(self) -> bool:
        """微信窗口出现后补检一次界面语言，变了就重建文案选择器。

        ``Uielements`` 里的会话列表、消息区、搜索框等描述符都是**导入时**按
        当时的语言建好的。启动时微信没开 → 语言检测失败 → 全用简体中文描述符；
        之后微信打开了，检测已经能出结果，但描述符不会自己更新，读会话就会
        "找不到列表"。这里在检测到主窗口时补一次。
        """
        try:
            from .Config import GlobalConfig
            from . import Uielements
        except Exception:  # pragma: no cover - 平台相关
            return False
        try:
            if GlobalConfig.language_detected:
                return False
            detected = GlobalConfig.refresh_language()
        except Exception as exc:  # pragma: no cover
            LOGGER.debug("language refresh failed: %s", exc)
            return False
        if not detected:
            return False
        language, version = detected, Uielements.Version
        Uielements.Main_window = Uielements.Main_window_Control(language=language, Version=version)
        Uielements.Login_window = Uielements.Login_window_Control(language=language, Version=version)
        Uielements.Independent_window = Uielements.Independent_window_Control(language=language, Version=version)
        Uielements.SideBar = Uielements.SideBar_Control(language=language, Version=version)
        Uielements.Buttons = Uielements.Button_Control(language=language, Version=version)
        Uielements.Edits = Uielements.Edit_Control(language=language, Version=version)
        Uielements.Texts = Uielements.Text_Control(language=language, Version=version)
        Uielements.TabItems = Uielements.TabItem_Control(language=language, Version=version)
        Uielements.Lists = Uielements.List_Control(language=language, Version=version)
        Uielements.Panes = Uielements.Pane_Control(language=language, Version=version)
        Uielements.Windows = Uielements.Window_Control(language=language, Version=version)
        Uielements.CheckBoxes = Uielements.CheckBox_Control(language=language, Version=version)
        Uielements.MenuItems = Uielements.MenuItem_Control(language=language, Version=version)
        Uielements.Menus = Uielements.Menu_Control(language=language, Version=version)
        Uielements.Groups = Uielements.Group_Control(language=language, Version=version)
        Uielements.Customs = Uielements.Custom_Control(language=language, Version=version)
        Uielements.ListItems = Uielements.ListItem_Control(language=language, Version=version)
        LOGGER.info("界面语言已重新检测为 %s，界面文案选择器已重建", language)
        return True

    def _find_window(self, Desktop, specs: tuple[dict[str, Any], ...]):
        """按一组描述符找窗口，**第一个存在的就是它**。找不到或查询出错都返回 None。

        查找失败**不能**被当成"窗口在"或"登录窗口在"：早前的写法把一次必然的
        ``AttributeError`` 吞掉后固定返回"等待扫码"，于是无论微信是什么状态，
        界面都显示请扫码 —— 一个永远不会自我纠正的假阳性。
        """
        for spec in specs:
            try:
                window = Desktop(backend="uia").window(**spec)
                if window.exists(timeout=self._timeout):
                    return window
                # 窗口被最小化到托盘时，UIA 里仍然找得到，只是不可见。
                # 只查 exists() 会把"我开着微信但收进了托盘"误报成
                # "微信没打开" —— 而收进托盘是商户的常态：他要一边聊天
                # 一边跑接单助手，没道理先把窗口摆出来才能用。
                if window.exists(timeout=0.2, visible_only=False):
                    return window
            except Exception as exc:  # pragma: no cover - 取决于本机 UIA
                LOGGER.debug("window lookup failed for %s: %s", spec, exc)
        return None

    def _login_window_state(self, Desktop) -> str | None:
        """区分「等待扫码」和「等待手机确认」。

        两者都停在同一个 ``mmui::LoginWindow`` 类里，只能靠子控件区分：
        扫码页有二维码图像和「扫码登录」文案；手机确认页有「确认登录」一类的
        按钮或「已扫描」文案。

        **没有登录窗口时返回 None**，由主窗口决定是不是已登录。只有登录窗口
        确实在、但结构认不出来时，才按等待扫码处理 —— 此时让商户去微信窗口
        看一眼总比谎报"已登录"安全。
        """
        window = self._find_window(Desktop, LOGIN_WINDOW_SPECS)
        if window is None:
            return None

        def has(**kwargs) -> bool:
            try:
                return bool(window.child_window(**kwargs).exists(timeout=0.1))
            except Exception:
                return False

        # 手机确认页通常出现「确认登录/手机确认」类文案或按钮。
        if has(control_type="Button", title_re=".*(确认登录|手机确认|继续登录).*") or has(
            control_type="Text", title_re=".*(已扫描|请在手机上确认).*"
        ):
            return WeChatState.WAITING_CONFIRM
        # 扫码页出现二维码图像或扫码提示。
        if has(control_type="Image") or has(control_type="Text", title_re=".*(扫码|二维码).*"):
            return WeChatState.WAITING_SCAN
        # 登录窗口确实在，但结构不认识：按等待扫码处理，不假装已登录。
        return WeChatState.WAITING_SCAN


class RealWeChatLauncher:
    """启动微信。仅在商户点击「打开微信」后调用。"""

    def launch(self) -> bool:
        import os

        try:
            from .Errors import NotInstalledError
            from .WeChatTools import Tools
        except Exception:
            return False
        try:
            path = Tools.where_weixin()
        except NotInstalledError:
            return False
        except Exception:
            return False
        if not path:
            return False
        try:
            os.startfile(path)
        except Exception:
            return False
        return True


# --------------------------------------------------------------------- 模拟


class SimulatedWeChat:
    """确定性的假微信，供演示与自动化测试使用。

    ``simulated=True`` 会一路带到界面和结果里 —— 演示数据不允许在没有任何
    提示的情况下落成正式经营订单。
    """

    def __init__(self, states: list[str] | None = None, launchable: bool = True, account_id: str = "wxid_demo"):
        self._states = list(states or [WeChatState.NOT_STARTED])
        self._launchable = launchable
        self.account_id = account_id
        self.launches = 0
        self._lock = threading.RLock()

    def advance(self) -> str:
        """推进到下一个状态，模拟商户在微信里完成动作。"""
        with self._lock:
            if len(self._states) > 1:
                self._states.pop(0)
            return self._states[0]

    def set_state(self, state: str) -> None:
        with self._lock:
            if self._states and self._states[0] == state:
                return
            self._states.insert(0, state)

    def probe(self) -> WeChatStatus:
        with self._lock:
            state = self._states[0]
        return WeChatStatus(
            state,
            f"模拟微信状态：{state}",
            account_id=self.account_id if state == WeChatState.LOGGED_IN else "",
            checked_at=time.time(),
            simulated=True,
        )

    def launch(self) -> bool:
        with self._lock:
            self.launches += 1
            if self._launchable and self._states[0] == WeChatState.NOT_STARTED:
                self._states[0] = WeChatState.WAITING_SCAN
            return self._launchable


# ----------------------------------------------------------------- 监测循环


class WeChatMonitor:
    """轮询式监测：等商户完成登录后自动进入下一步。

    单线程、有事件、可停止。绝不会叠加出无限增长的轮询线程 —— 每个监测器
    只有一个后台线程，``stop()`` 之后 ``start()`` 不会留下孤儿。
    """

    def __init__(
        self,
        probe: WeChatProbe,
        launcher: WeChatLauncher | None = None,
        interval: float = 2.0,
        on_change: Callable[[WeChatStatus], None] | None = None,
        on_tick: Callable[[WeChatStatus], None] | None = None,
        ui_lock: Any = None,
    ):
        self.probe = probe
        self.launcher = launcher
        self.interval = max(0.2, float(interval))
        self.on_change = on_change
        self.on_tick = on_tick
        #: 与读历史、持续监听共用同一把锁。忙的时候不去碰微信窗口。
        self._ui_lock = ui_lock
        self.latest = WeChatStatus(WeChatState.UNREADABLE, "尚未检测")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def check_once(self) -> WeChatStatus:
        """同步检测一次，立即返回。任何时候都可以安全调用。

        **读取历史期间这一轮直接跳过。** 探测本身要驱动微信窗口，和读历史
        共用同一个窗口 —— 两个线程同时动它，行为不可预测。

        ⚠️ **这个改动本身是对的，但它不是"读到 0 条"的原因。** 专门做过
        对照实验（`examples/real_race_repro.py`）：让监视器每 2 秒并发探测、
        与读取同时进行，修复前和修复后**都稳定读到 7 条**，没能复现空读。
        所以真正让读取返回 0 的原因**仍未查明**。这里只是去掉一个本来就
        不该有的竞争，不是在修那个已知故障。
        """
        if self._ui_lock is not None:
            try:
                if self._ui_lock.busy:
                    return self.latest
            except Exception:  # pragma: no cover - 队列实现差异
                pass
        try:
            status = self.probe.probe()
        except Exception as exc:
            status = WeChatStatus(WeChatState.UNREADABLE, f"检测微信状态时出错：{exc}", checked_at=time.time())
        previous = self.latest.state
        self.latest = status
        if self.on_change and status.state != previous:
            try:
                self.on_change(status)
            except Exception:
                pass
        if self.on_tick:
            try:
                self.on_tick(status)
            except Exception:
                pass
        return status

    def launch(self) -> bool:
        if self.launcher is None:
            return False
        ok = bool(self.launcher.launch())
        # 启动后立即复查一次，让界面尽快反映新状态。
        if ok:
            self.check_once()
        return ok

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="wechat-monitor", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.check_once()
            if self.latest.ready:
                return
            self._stop.wait(self.interval)

    def __enter__(self) -> "WeChatMonitor":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()


__all__ = [
    "RealWeChatLauncher",
    "RealWeChatProbe",
    "STATE_COPY",
    "SimulatedWeChat",
    "WeChatLauncher",
    "WeChatMonitor",
    "WeChatProbe",
    "WeChatState",
    "WeChatStatus",
]
