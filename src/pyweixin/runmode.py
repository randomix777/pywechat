"""运行模式与本地设置。

三种模式，商户用中文就能选：

* ``background`` **后台整理** —— 读取和分析、记单，**不发任何消息**。这是
  默认模式，也是唯一不需要商户额外确认就能用的模式。
* ``auto_take`` **自动接单与回复** —— 首次对指定会话开启后，普通订单不再
  逐单确认，自动记单并发送回执。
* ``paused`` **暂停** —— 立即停止发送。已经排队但没发出的回执留在队列里，
  恢复后继续，不会丢也不会重复。

另外有一道独立的开关：**是否允许本机被自动接单占用**。未开启时即使选了
``auto_take`` 也不会发送 —— 因为自动回复要操作微信窗口，可能影响正在用电脑
的商户。这道开关必须显式打开，不能默认打开。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .conversations import Conversation


class RunMode:
    BACKGROUND = "background"
    AUTO_TAKE = "auto_take"
    PAUSED = "paused"

    ALL = (BACKGROUND, AUTO_TAKE, PAUSED)

    COPY = {
        BACKGROUND: ("后台整理", "读取并分析消息、记单，不发送任何消息。"),
        AUTO_TAKE: ("自动接单与回复", "普通订单自动记单并发送一次回执。"),
        PAUSED: ("已暂停", "已停止发送。恢复后继续发送未发出的回执。"),
    }


#: 短消息合并窗口：同一客户连续几条短消息合成一单，避免刷屏。
DEFAULT_MERGE_WINDOW_SECONDS = 45.0
DEFAULT_MAX_MERGE_WAIT_SECONDS = 90.0


@dataclass
class AutoOrderSettings:
    """自动接单的可配置项。全部有安全默认值。"""

    mode: str = RunMode.BACKGROUND
    #: 是否允许本机被自动接单占用。未开启时永远不发送。
    allow_local_automation: bool = False
    #: 缺价时是否允许先记单、标价格待定，而不是转人工。
    allow_price_pending: bool = False
    #: 单笔金额上限，超过转人工。None 表示不限制。
    max_order_amount: float | None = 5000.0
    #: 单行数量上限。
    max_line_quantity: float = 200.0
    #: 连续短消息合并窗口。
    merge_window_seconds: float = DEFAULT_MERGE_WINDOW_SECONDS
    #: 合并等待上限。无论客户发多少条，最多等这么久。
    max_merge_wait_seconds: float = DEFAULT_MAX_MERGE_WAIT_SECONDS
    #: 自动发送回执。关掉后仍然自动记单，只是不发消息。
    auto_reply: bool = True

    def __post_init__(self) -> None:
        if self.mode not in RunMode.ALL:
            raise ValueError(f"unknown run mode: {self.mode}")
        if self.merge_window_seconds < 0 or self.max_merge_wait_seconds < 0:
            raise ValueError("merge windows must not be negative")
        if self.max_merge_wait_seconds < self.merge_window_seconds:
            # 上限小于窗口是配置错误，会让"最多等这么久"变成空话。
            self.max_merge_wait_seconds = self.merge_window_seconds

    @property
    def sending_allowed(self) -> bool:
        """能不能发消息。两个条件都必须满足。"""
        return self.mode == RunMode.AUTO_TAKE and self.allow_local_automation and self.auto_reply

    @property
    def recording_allowed(self) -> bool:
        """能不能自动记单。后台整理模式也记单，只是不发。"""
        return self.mode in (RunMode.BACKGROUND, RunMode.AUTO_TAKE)

    def label(self) -> str:
        return RunMode.COPY[self.mode][0]

    def describe(self) -> str:
        if self.mode == RunMode.AUTO_TAKE and not self.allow_local_automation:
            return "自动接单与回复（尚未允许本机自动操作，暂不发送）"
        return RunMode.COPY[self.mode][1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allow_local_automation": self.allow_local_automation,
            "allow_price_pending": self.allow_price_pending,
            "max_order_amount": self.max_order_amount,
            "max_line_quantity": self.max_line_quantity,
            "merge_window_seconds": self.merge_window_seconds,
            "max_merge_wait_seconds": self.max_merge_wait_seconds,
            "auto_reply": self.auto_reply,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AutoOrderSettings":
        defaults = cls()
        return cls(
            mode=str(data.get("mode", defaults.mode)),
            allow_local_automation=bool(data.get("allow_local_automation", defaults.allow_local_automation)),
            allow_price_pending=bool(data.get("allow_price_pending", defaults.allow_price_pending)),
            max_order_amount=(
                None if data.get("max_order_amount") is None else float(data["max_order_amount"])
            ),
            max_line_quantity=float(data.get("max_line_quantity", defaults.max_line_quantity)),
            merge_window_seconds=float(data.get("merge_window_seconds", defaults.merge_window_seconds)),
            max_merge_wait_seconds=float(data.get("max_merge_wait_seconds", defaults.max_merge_wait_seconds)),
            auto_reply=bool(data.get("auto_reply", defaults.auto_reply)),
        )


@dataclass
class MerchantSettings:
    """跨启动保留的配置。向导走完后不再重走。"""

    account_id: str = ""
    live_conversation: str = ""
    history_conversations: list[str] = field(default_factory=list)
    history_range: str = "last_week"
    history_max_messages: int = 200
    auto: AutoOrderSettings = field(default_factory=AutoOrderSettings)
    #: 向导是否已完成。微信账号切换时会被重置。
    wizard_complete: bool = False
    #: 上次用的模型**产品别名**。换模型只影响新任务，已在跑的不变。
    model_alias: str = ""
    #: 上次用的网关地址。换账号时一并清空。
    gateway_url: str = ""
    #: 上次见到的微信版本号。微信一升级，界面选择器、读取逻辑都可能
    #: 对不上 —— 先记下来，变了就提醒商户重走一次检测，而不是静默读空。
    wechat_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "live_conversation": self.live_conversation,
            "history_conversations": list(self.history_conversations),
            "history_range": self.history_range,
            "history_max_messages": self.history_max_messages,
            "auto": self.auto.to_dict(),
            "wizard_complete": self.wizard_complete,
            "model_alias": self.model_alias,
            "gateway_url": self.gateway_url,
            "wechat_version": self.wechat_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MerchantSettings":
        return cls(
            account_id=str(data.get("account_id", "")),
            live_conversation=str(data.get("live_conversation", "")),
            history_conversations=[str(item) for item in data.get("history_conversations", ())],
            history_range=str(data.get("history_range", "last_week")),
            history_max_messages=int(data.get("history_max_messages", 200)),
            auto=AutoOrderSettings.from_dict(data.get("auto") or {}),
            wizard_complete=bool(data.get("wizard_complete", False)),
            model_alias=str(data.get("model_alias", "")),
            gateway_url=str(data.get("gateway_url", "")),
            wechat_version=str(data.get("wechat_version", "")),
        )

    def reset_for_new_account(self) -> None:
        """微信账号切换时调用。

        账号变了，旧账号的会话绑定、客户记忆和订单一律不能混用。
        模型选择是**账号无关**的产品偏好，刻意保留 —— 换微信账号不等于
        换模型，也不该让商户重新选一次。
        """
        self.account_id = ""
        self.live_conversation = ""
        self.history_conversations = []
        self.wizard_complete = False


class SettingsStore:
    """原子读写的本地配置。

    先写临时文件再替换，避免商户在断电或强制关窗时得到一个半截的配置文件。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def load(self) -> MerchantSettings:
        with self._lock:
            if not self.path.exists():
                return MerchantSettings()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # 配置坏了不能让软件起不来：回到默认值，让商户重走一次向导。
                return MerchantSettings()
        return MerchantSettings.from_dict(data if isinstance(data, dict) else {})

    def save(self, settings: MerchantSettings) -> None:
        with self._lock:
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(settings.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)


__all__ = [
    "DEFAULT_MAX_MERGE_WAIT_SECONDS",
    "DEFAULT_MERGE_WINDOW_SECONDS",
    "AutoOrderSettings",
    "MerchantSettings",
    "RunMode",
    "SettingsStore",
]
