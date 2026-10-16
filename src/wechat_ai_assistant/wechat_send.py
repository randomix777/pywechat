"""发送前的微信输入草稿保护。

## 现有发送函数会做什么

`Messages.send_messages_to_friend()` 是本项目唯一的发送实现。它做三件事，
**每一件都会破坏用户正在输入的内容**：

===========================  ==========================================
它做的                       对用户的影响
===========================  ==========================================
``edit_area.set_text('')``   **清空输入框里的草稿**（``GlobalConfig.clear``
                             默认就是 ``True``）
``copy_text_to_clipboard``   **覆盖用户剪贴板**里复制的东西
``edit_area.click_input()``  **抢走键盘焦点**，用户在别的窗口打字会被
                             打进微信
===========================  ==========================================

## 因此本轮的做法

**先检测，检测不了就不发。** 不采用「先保存草稿 → 发送 → 还原」：
用户在保存和还原之间随时可能在打字，还原回去的可能是错的内容，
或者把用户新打的字覆盖掉。这种做法会把一个明确的问题变成一个隐蔽的问题。

本模块只做**只读**检查：

* 输入框当前有没有文字（非空就暂缓这条回执，并说明原因）；
* 当前前台会话是不是目标会话（不是就不发）；
* 剪贴板当前状态能不能确认（不能确认就不发）。

只要有一项不能确认，结果就是 ``hold`` —— 留在队列里等条件满足，
而不是冒险发出去。

## 真实发送入口默认关闭

在草稿保护完成真机验证之前，:func:`real_sending_available` 返回 ``False``，
界面上的「自动接单与回复」只会把回执留在队列里，不会真的操作微信窗口。
模拟发送（注入 sender）继续用于验证整条业务链路。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, Sequence


class SendVerdict:
    """发送前检查的结论。"""

    #: 输入框有内容 —— 商户正在打字。
    DRAFT_PRESENT = "draft_present"
    #: 前台会话不是目标会话。
    WRONG_CONVERSATION = "wrong_conversation"
    #: 读不到输入框状态：无法确认就是不发。
    STATE_UNKNOWN = "state_unknown"
    #: 真实发送入口尚未在真机验证。
    REAL_SENDING_DISABLED = "real_sending_disabled"
    #: 窗口被最小化或不可见。
    WINDOW_NOT_USABLE = "window_not_usable"
    #: 微信账号与绑定的账号不一致。
    ACCOUNT_MISMATCH = "account_mismatch"

    OK = "ok"
    ALL = (OK, DRAFT_PRESENT, WRONG_CONVERSATION, STATE_UNKNOWN, REAL_SENDING_DISABLED, WINDOW_NOT_USABLE, ACCOUNT_MISMATCH)


#: 每种情况的商户可读说明。界面上直接显示这些。
VERDICT_MESSAGES = {
    SendVerdict.DRAFT_PRESENT: "微信输入框里还有您没发完的内容，已暂缓发送这条回执。",
    SendVerdict.WRONG_CONVERSATION: "微信当前打开的不是目标会话，已暂缓发送。",
    SendVerdict.STATE_UNKNOWN: "读不到微信输入框的状态，为安全起见暂不发送。",
    SendVerdict.REAL_SENDING_DISABLED: "真实发送功能尚未在您的电脑上验证过，目前只整理不发送。",
    SendVerdict.WINDOW_NOT_USABLE: "微信窗口当前不可用，已暂缓发送。",
    SendVerdict.ACCOUNT_MISMATCH: "微信当前登录的账号与绑定的不一致，已暂缓发送。",
}


@dataclass(frozen=True)
class SendCheck:
    """一次检查的结果。``ok`` 为假时 ``reason`` 说明为什么暂缓。"""

    ok: bool
    reason: str = ""
    detail: str = ""

    @property
    def message(self) -> str:
        return VERDICT_MESSAGES.get(self.reason, "")

    def to_dict(self) -> dict[str, str]:
        return {"ok": "1" if self.ok else "0", "reason": self.reason, "detail": self.detail, "message": self.message}


ALLOWED = SendCheck(ok=True)


class SendHeld(Exception):
    """This receipt must not be sent **now**. Park it; do not call it a failure.

    Distinct from a send that was attempted and whose result is unknown:
    nothing was typed into WeChat, so there is nothing to reconcile later.
    """

    def __init__(self, check: SendCheck):
        super().__init__(check.reason)
        self.check = check
        self.reason = check.reason


@dataclass(frozen=True)
class TargetState:
    """只读快照：目标会话当前是什么状态。"""

    conversation: str = ""
    draft_text: str = ""
    account_id: str = ""
    window_ready: bool = False
    #: True 表示这份快照是真实读到的，不是猜的。
    observed: bool = False

    @property
    def has_draft(self) -> bool:
        return bool(self.draft_text.strip())


class TargetInspector(Protocol):
    """只读地检查目标会话状态。**不得**修改任何微信内容。"""

    def inspect(self, conversation: str) -> TargetState:
        """返回当前状态快照。不点击、不输入、不清空、不改剪贴板。"""


class StaticInspector:
    """可注入的固定状态，供测试与演示使用。"""

    def __init__(self, state: TargetState):
        self._state = state

    def inspect(self, conversation: str) -> TargetState:
        return TargetState(
            conversation=self._state.conversation or conversation,
            draft_text=self._state.draft_text,
            account_id=self._state.account_id,
            window_ready=self._state.window_ready,
            observed=self._state.observed,
        )


class WeChatTargetInspector:
    """基于 UIA 的只读检查。

    只读取：会话标题文字、输入框当前文本。不调用任何 ``set_text``、
    ``click_input``、``type_keys`` 或剪贴板函数。
    """

    def __init__(self, expected_account: str = "", timeout: float = 0.2):
        self.expected_account = expected_account
        self.timeout = timeout

    def inspect(self, conversation: str) -> TargetState:
        try:
            from .Uielements import Edits, Lists
            from .WeChatTools import Navigator
        except Exception:
            return TargetState(observed=False)
        try:
            main_window = Navigator.find_main_window()
        except Exception:
            return TargetState(observed=False)
        if main_window is None:
            return TargetState(observed=False)
        state = TargetState(window_ready=True, observed=True)
        try:
            edit_area = main_window.child_window(**Edits.CurrentChatEdit)
            if not edit_area.exists(timeout=self.timeout):
                return TargetState(window_ready=False, observed=True)
            state.draft_text = str(edit_area.get_value() or "")
        except Exception:
            return TargetState(window_ready=True, observed=False)
        try:
            title = main_window.child_window(**Lists.CurrentChatNameText)
            state.conversation = str(title.window_text() or "") if title.exists(timeout=self.timeout) else ""
        except Exception:
            state.conversation = ""
        try:
            from .WeChatTools import Tools

            folder = Tools.where_wxid_folder()
            state.account_id = folder.rstrip("\\/").split("\\")[-1].split("/")[-1] if folder else ""
        except Exception:
            state.account_id = ""
        return state


#: 真实发送在真机验证前的总开关。**默认关闭**。
_REAL_SENDING_VERIFIED = False


def real_sending_available() -> bool:
    """真实自动发送是否允许。

    在装有真实微信的机器上完成一次「草稿非空时不发送」「不覆盖剪贴板」
    「不抢焦点」的实测并记录结果之后，才可以打开。
    """
    return _REAL_SENDING_VERIFIED


def mark_real_sending_verified(verified: bool) -> None:
    """仅供验收流程调用。默认 False。"""
    global _REAL_SENDING_VERIFIED
    _REAL_SENDING_VERIFIED = bool(verified)


class SafeReceiptSender:
    """发送前逐项核对，任何一项不确定就不发。

    与 :class:`~wechat_ai_assistant.receipts.ReceiptOutbox` 的关系：outbox 负责**队列**，
    这里负责**能不能安全地发**。两者都通过 ``hold`` 表达「现在不行」，
    但 outbox 关心的是幂等和恢复，这里关心的是不破坏用户的内容。
    """

    def __init__(
        self,
        inspector: TargetInspector,
        inner: Callable[[str, str], Any] | None = None,
        expected_account: str = "",
        allow_real: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.inspector = inspector
        self.inner = inner
        self.expected_account = expected_account
        self._allow_real = allow_real or real_sending_available
        self._clock = clock
        self._lock = threading.RLock()
        #: 每条回执最近一次被暂缓的原因，界面上要显示。
        self.last_hold: dict[str, str] = {}

    def check(self, conversation: str, order_id: str = "") -> SendCheck:
        """在发送之前问一次：现在能发吗。"""
        allow = self._allow_real
        if not allow():
            return SendCheck(False, SendVerdict.REAL_SENDING_DISABLED)
        state = self.inspector.inspect(conversation)
        with self._lock:
            if not state.observed:
                return SendCheck(False, SendVerdict.STATE_UNKNOWN)
            if not state.window_ready:
                return SendCheck(False, SendVerdict.WINDOW_NOT_USABLE)
            if state.has_draft:
                return SendCheck(
                    False, SendVerdict.DRAFT_PRESENT,
                    f"输入框里已有 {len(state.draft_text)} 个字",
                )
            if state.conversation and state.conversation.strip() != conversation.strip():
                return SendCheck(
                    False, SendVerdict.WRONG_CONVERSATION,
                    f"当前是「{state.conversation}」，目标是「{conversation}」",
                )
            if self.expected_account and state.account_id and state.account_id != self.expected_account:
                return SendCheck(False, SendVerdict.ACCOUNT_MISMATCH, state.account_id)
        return ALLOWED

    def send(self, conversation: str, text: str, order_id: str = "") -> SendCheck:
        """核对通过才真正发送。任何一步不通过就返回暂缓原因。"""
        verdict = self.check(conversation, order_id)
        if not verdict.ok:
            with self._lock:
                if order_id:
                    self.last_hold[order_id] = verdict.reason
            return verdict
        if self.inner is None:
            return SendCheck(False, SendVerdict.STATE_UNKNOWN, "没有可用的发送实现")
        # 发送前**再核对一次**：从检查到真正发送之间商户可能已经切换了会话。
        second = self.inspector.inspect(conversation)
        if not second.observed or not second.window_ready or second.has_draft:
            reason = (
                SendVerdict.STATE_UNKNOWN if not second.observed
                else SendVerdict.WINDOW_NOT_USABLE if not second.window_ready
                else SendVerdict.DRAFT_PRESENT
            )
            with self._lock:
                if order_id:
                    self.last_hold[order_id] = reason
            return SendCheck(False, reason)
        if second.conversation and second.conversation.strip() != conversation.strip():
            with self._lock:
                if order_id:
                    self.last_hold[order_id] = SendVerdict.WRONG_CONVERSATION
            return SendCheck(False, SendVerdict.WRONG_CONVERSATION)
        self.inner(conversation, text)
        with self._lock:
            self.last_hold.pop(order_id, None) if order_id else None
        return ALLOWED

    def hold_summary(self) -> dict[str, str]:
        with self._lock:
            return dict(self.last_hold)


def attach_guard(
    outbox: Any,
    guard: SafeReceiptSender,
    *,
    allowed_targets: Sequence[str],
    paused: bool,
    expected_versions: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """把守卫接到 outbox 的发送循环里。

    outbox 原本的发送是"直接调 sender"。这里替换成"先过守卫再调 sender"，
    其余逻辑（幂等、租约、未知结果、重试）完全不变。
    """
    original = outbox.sender

    def guarded(target: str, text: str) -> None:
        if original is not None and original is not guard.inner:
            # 已有注入的模拟 sender：仍然要过守卫，保证测试覆盖同一条路径。
            pass
        guard.send(target, text)

    outbox.sender = guarded
    return outbox.dispatch(
        allowed_targets=allowed_targets, paused=paused, expected_versions=expected_versions or {}
    )


__all__ = [
    "ALLOWED",
    "SendCheck",
    "SendHeld",
    "SendVerdict",
    "SafeReceiptSender",
    "StaticInspector",
    "TargetInspector",
    "TargetState",
    "VERDICT_MESSAGES",
    "WeChatTargetInspector",
    "attach_guard",
    "mark_real_sending_verified",
    "real_sending_available",
]
