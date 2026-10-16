"""会话发现、搜索与同名消歧。

商户不该被迫知道哪个群叫"default"，也不该在两个同名群之间猜。发现结果里
每一个会话都带一个**稳定的消歧标识**，执行任何操作前都用它核对目标。

## 不承诺的事

会话列表来自微信的「最近会话」/「通讯录」，因此：

* **不包含**所有联系人和所有历史；
* 某些会话可能因为很久没聊而不在列表里；
* 群成员数量可能读不到。

这些在界面上如实显示，不假装"已经拿到全部"。

## 同名消歧

微信里两个群可以重名。``Conversation.disambiguation`` 由
``(名称, 群成员数, 序号)`` 组成：同名同人数再出现第三个时，序号区分它们。
执行前（读历史、发回执）都要用 ``matches()`` 核对，核对不上就拒绝执行。
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence


class ConversationKind:
    GROUP = "group"
    FRIEND = "friend"
    UNKNOWN = "unknown"

    ALL = (GROUP, FRIEND, UNKNOWN)


@dataclass(frozen=True)
class Conversation:
    """一个可被选择和核对的会话。"""

    name: str
    kind: str = ConversationKind.UNKNOWN
    #: 群成员数。读不到时为 0，是 0 不代表是好友。
    member_count: int = 0
    #: 发现顺序。同名时用它稳定地区分，而不是让界面顺序漂移。
    ordinal: int = 0
    #: 微信侧可用于二次核对的标识；读不到时为空，界面会显示"无法二次核对"。
    wxid: str = ""
    #: 最后活跃时间（会话表里的 last_timestamp，秒）。0 = 不知道，
    #: 界面把不知道的排在后面。有它的会话按微信一样：越新越靠上。
    #: 注意：它不进 identity —— 时间一直在变，进去身份就永不稳定了。
    last_active: int = 0

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("conversation name must not be empty")
        if self.kind not in ConversationKind.ALL:
            raise ValueError(f"unknown conversation kind: {self.kind}")

    @property
    def disambiguation(self) -> str:
        """人类可读的消歧标识，直接显示在界面上。"""
        if self.kind == ConversationKind.GROUP and self.member_count:
            return f"{self.name}（{self.member_count}人）"
        if self.wxid:
            return f"{self.name}（{self.wxid}）"
        return self.name

    @property
    def identity(self) -> str:
        """机器可读的身份。用于绑定、幂等和执行前核对。"""
        raw = f"{self.name}|{self.kind}|{self.member_count}|{self.wxid}|{self.ordinal}"
        return "cv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def matches(self, other: "Conversation | str") -> bool:
        """执行前核对目标是否仍然是选中的那个会话。"""
        if isinstance(other, str):
            return other in (self.identity, self.name)
        return other.identity == self.identity

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "member_count": self.member_count,
            "ordinal": self.ordinal,
            "wxid": self.wxid,
            "last_active": self.last_active,
            "disambiguation": self.disambiguation,
            "identity": self.identity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conversation":
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", ConversationKind.UNKNOWN)),
            member_count=int(data.get("member_count", 0)),
            ordinal=int(data.get("ordinal", 0)),
            wxid=str(data.get("wxid", "")),
            last_active=int(data.get("last_active", 0) or 0),
        )


def disambiguate(entries: Sequence[Conversation]) -> tuple[Conversation, ...]:
    """给同名会话加上稳定序号，并排序：有新消息的在上（和微信一致），
    不知道时间的保持原来的名字排序。

    排序是必要的：界面列表顺序如果随发现顺序抖动，商户会以为自己换了群。
    没时间戳时退回 (名称, 人数) —— 老行为原样保留。
    """
    ordered = sorted(entries, key=lambda c: (-int(c.last_active or 0), c.name, c.member_count))
    seen: dict[tuple[str, int], int] = {}
    result: list[Conversation] = []
    for item in ordered:
        key = (item.name, item.member_count)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            result.append(
                Conversation(
                    name=item.name,
                    kind=item.kind,
                    member_count=item.member_count,
                    ordinal=seen[key],
                    wxid=item.wxid,
                )
            )
        else:
            result.append(item)
    return tuple(result)


def duplicates(entries: Sequence[Conversation]) -> tuple[str, ...]:
    """返回出现多次的会话名称，界面据此提示"有同名会话，请确认"。"""
    counts: dict[str, int] = {}
    for item in entries:
        counts[item.name] = counts.get(item.name, 0) + 1
    return tuple(sorted(name for name, count in counts.items() if count > 1))


class ConversationSource(Protocol):
    """会话列举来源。可注入，因此界面和测试都不需要真实微信。"""

    def list_conversations(self, kind: str = "") -> tuple[Conversation, ...]:
        """返回当前可读会话。``kind`` 为空表示全部类型。"""


class RealConversationSource:
    """真实微信会话列举。

    三条路，按顺序试：
    1. 解密库（``wcdb_fn``）：最近会话 + 全量群名 + 人数，全是真名；
    2. 内存 carve（``localdb``）：不碰微信窗口，UIA 全死时这是唯一路；
    3. 界面通讯录（``Contacts.get_recent_groups``）：老版本微信才走得通。

    解密和内存都只读不写，但仍**必须**在 UI 串行队列里调用，
    不能和发送、监听并发。任何一条失败都不致命：能列多少列多少，
    界面会如实说明来源。
    """

    def __init__(self, ui_lock: Any = None,
                 wcdb_fn: Callable[[str], Sequence[Any] | None] | None = None):
        self._ui_lock = ui_lock
        #: 解密库会话来源（应用层注入，缺密钥时为 None）。抛异常=不可用，
        #: 这里吞掉回落内存路，不把整次列举带崩。
        self.wcdb_fn = wcdb_fn

    def _guarded(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._ui_lock is None:
            return function(*args, **kwargs)
        with self._ui_lock:
            return function(*args, **kwargs)

    def list_conversations(self, kind: str = "") -> tuple[Conversation, ...]:
        from . import localdb
        from .WeChatAuto import Contacts

        found: list[Conversation] = []
        try:
            from .wechat_state import uia_automation_available
            uia_ok = uia_automation_available()
        except Exception:
            uia_ok = True
        if self.wcdb_fn is not None:
            # 解密库优先：真名 + 人数 + 最近时间，一次到位。
            # 失败（没密钥、文件锁、验签不过）就当没这条路。
            try:
                rows = self._guarded(self.wcdb_fn, kind) or ()
            except Exception:
                rows = ()
            for item in rows:
                username = str(getattr(item, "username", "") or "")
                if not username:
                    continue
                show = str(getattr(item, "display", "") or "") or username
                item_kind = str(getattr(item, "kind", "") or "")
                if item_kind not in ConversationKind.ALL:
                    item_kind = (ConversationKind.GROUP if username.endswith("@chatroom")
                                 else ConversationKind.FRIEND)
                try:
                    members = int(getattr(item, "member_count", 0) or 0)
                except (TypeError, ValueError):
                    members = 0
                try:
                    active = int(getattr(item, "last_timestamp", 0) or 0)
                except (TypeError, ValueError):
                    active = 0
                found.append(Conversation(name=show, kind=item_kind,
                                          member_count=members, wxid=username,
                                          last_active=active))
            if found:
                return disambiguate(found)
        memory_groups: list[tuple[str, str]] = []
        if kind in ("", ConversationKind.GROUP):
            # 内存优先：不碰微信窗口，4.1.15 上 UIA 全死时这是唯一路。
            # 人数读不到填 0，界面照群显示，不写"联系人"。
            try:
                memory_groups = self._guarded(localdb.list_group_conversations) or []
            except Exception:
                memory_groups = []
            for name, wxid in memory_groups:
                found.append(Conversation(name=str(name), kind=ConversationKind.GROUP,
                                          member_count=0, wxid=str(wxid)))
        if kind in ("", ConversationKind.GROUP) and not memory_groups:
            try:
                for name, count in self._guarded(Contacts.get_recent_groups, is_maximize=False, close_weixin=False) or ():
                    found.append(Conversation(name=str(name), kind=ConversationKind.GROUP, member_count=int(count or 0)))
            except Exception:
                # 读不到群列表不是致命错误：好友仍然可选，界面会说明。
                pass
        if kind in ("", ConversationKind.FRIEND) and uia_ok:
            # 好友名单只有 UIA 一条路：界面已死就直接跳过，别让人干等超时。
            try:
                for name in self._guarded(Contacts.get_friends_info, is_maximize=False, close_weixin=False) or ():
                    found.append(Conversation(name=str(name), kind=ConversationKind.FRIEND))
            except Exception:
                pass
        return disambiguate(found)


class SimulatedConversations:
    """确定性的假会话列表，含重名与搜索场景。"""

    DEFAULT = (
        ("鲜果批发群", ConversationKind.GROUP, 128),
        ("鲜果批发群", ConversationKind.GROUP, 256),
        ("社区团购", ConversationKind.GROUP, 64),
        ("王老板", ConversationKind.FRIEND, 0),
        ("李女士", ConversationKind.FRIEND, 0),
        ("夜宵烧烤群", ConversationKind.GROUP, 42),
    )

    def __init__(self, entries: Sequence[tuple[str, str, int]] | None = None):
        self._entries = list(entries if entries is not None else self.DEFAULT)

    def list_conversations(self, kind: str = "") -> tuple[Conversation, ...]:
        return disambiguate(
            Conversation(name=name, kind=entry_kind, member_count=count)
            for name, entry_kind, count in self._entries
            if kind in ("", entry_kind)
        )


def search(entries: Sequence[Conversation], query: str) -> tuple[Conversation, ...]:
    """按名称模糊搜索。空查询返回全部。"""
    text = (query or "").strip()
    if not text:
        return tuple(entries)
    lowered = text.lower()
    return tuple(item for item in entries if lowered in item.name.lower() or lowered in item.disambiguation.lower())


class ConversationRegistry:
    """商户的选择结果，可持久化、可恢复。

    第一版实时接单只允许**一个**会话；历史读取允许多选。这个限制写在数据
    结构里，而不是靠界面提示——否则换一个入口就会绕过它。
    """

    def __init__(self, entries: Sequence[Conversation] = ()):
        self.available: tuple[Conversation, ...] = tuple(entries)
        self.history_selected: list[Conversation] = []
        self.live_selected: Conversation | None = None

    def refresh(self, entries: Sequence[Conversation]) -> None:
        self.available = disambiguate(entries)

    def set_history(self, selected: Sequence[Conversation]) -> None:
        self.history_selected = [item for item in selected]

    def set_live(self, conversation: Conversation | None) -> None:
        if conversation is None:
            self.live_selected = None
            return
        if not self.contains(conversation):
            raise ValueError("cannot select a conversation that was not discovered")
        self.live_selected = conversation

    def contains(self, conversation: Conversation) -> bool:
        return any(item.matches(conversation) for item in self.available)

    def resolve_history(self, identities: Sequence[str]) -> list[Conversation]:
        """把持久化的身份还原成会话对象；找不到的跳过并保留原值以便提示。"""
        resolved: list[Conversation] = []
        for identity in identities:
            match = next((item for item in self.available if item.identity == identity), None)
            if match is not None:
                resolved.append(match)
        return resolved

    def has_ambiguous_names(self) -> tuple[str, ...]:
        return duplicates(self.available)

    def to_dict(self) -> dict[str, Any]:
        return {
            "history": [item.identity for item in self.history_selected],
            "live": self.live_selected.identity if self.live_selected else "",
        }

    def available_to_dicts(self) -> list[dict[str, Any]]:
        """把当前可见的会话列表转成可落盘的字典。"""
        return [item.to_dict() for item in self.available]

    def restore_available(self, rows: Any) -> int:
        """从落盘数据恢复可见列表，返回恢复条数。坏行跳过，不断整个恢复。"""
        items: list[Conversation] = []
        for row in rows or ():
            try:
                item = Conversation.from_dict(row)
            except Exception:
                continue
            if item.name.strip():
                items.append(item)
        self.available = tuple(items)
        return len(items)

    @classmethod
    def from_dict(cls, data: dict[str, Any], available: Sequence[Conversation]) -> "ConversationRegistry":
        registry = cls(available)
        registry.history_selected = registry.resolve_history(data.get("history", ()))
        live = str(data.get("live", ""))
        if live:
            registry.live_selected = next((item for item in available if item.identity == live), None)
        return registry


__all__ = [
    "Conversation",
    "ConversationKind",
    "ConversationRegistry",
    "ConversationSource",
    "RealConversationSource",
    "SimulatedConversations",
    "disambiguate",
    "duplicates",
    "search",
]
