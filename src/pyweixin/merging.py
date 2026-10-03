"""短消息合并：把同一个人连着发的几句话合成一次解析。

## 为什么

客户在群里下单常常是分三句发的：

```
09:00:01  张姐：苹果两箱
09:00:04  张姐：再加一箱
09:00:09  张姐：明天送
```

逐条解析会产生三笔变更、三次计费、三条回执，客户在群里看到刷屏。
合并成一次之后只有一笔订单、一次回执。

## 分组键

``(账号, 会话, 发送者, 话题)``

* **账号**、**会话**：不同群不同人互不干扰。
* **发送者**：默认按人分组 —— 两个人同时在群里下单不该被合成一笔。
* **话题**：有引用关系时按引用分组（接龙场景），没有就归到该发送者的默认话题。

## 两条时间线

* **静默窗口**：最后一条消息之后安静这么久就结算。
* **最大等待**：第一条消息之后最多等这么久。客户连发 50 条也不能一直等。

两条都**可调**，且默认值只是起点 —— 还没有真实商户数据能证明 45/90 秒合适。

## 不重复解析、计费、记单、发送

合并完成后，原始消息会被标记为 ``merged``，代表消息携带
``merged_message_ids``。解析入口**只处理代表消息**，原始消息直接跳过：

* 解析只发生一次（计费只发生一次）；
* 订单只建一笔；
* 回执只发一条。

这个标记持久化，因此**重启后也不会**把原始消息再解析一遍。

## 迟到与重复

* **迟到**：一条早于已结算窗口的消息不会被塞回已关闭的批次（那会让已发出的
  回执与订单对不上），而是进入下一个批次，并在证据里标明它是迟到的。
* **重复**：按 ``event_id`` 去重，重复投递不会产生第二条。

## 证据映射

每个合并结果都保留 ``merged_message_ids``，订单的证据可以回溯到**每一条**
原始消息，而不是只指向合成后的那一段文字。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

#: 合并组在库里的状态。
GROUP_OPEN = "open"
GROUP_CLOSED = "closed"

#: 原始消息的合并状态。
MESSAGE_PENDING = "pending"
MESSAGE_MERGED = "merged"
MESSAGE_REPRESENTATIVE = "representative"


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class MergeKey:
    """分组键。话题为空表示该发送者的默认话题。"""

    account_id: str
    conversation: str
    sender: str
    topic: str = ""

    def __post_init__(self) -> None:
        if not str(self.conversation).strip():
            raise ValueError("merge key requires a conversation")
        if not str(self.sender).strip():
            raise ValueError("merge key requires a sender")

    def to_dict(self) -> dict[str, str]:
        return {
            "account_id": self.account_id,
            "conversation": self.conversation,
            "sender": self.sender,
            "topic": self.topic,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MergeKey":
        return cls(
            account_id=str(data.get("account_id", "default")),
            conversation=str(data.get("conversation", "")),
            sender=str(data.get("sender", "")),
            topic=str(data.get("topic", "")),
        )

    @property
    def storage_key(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass
class BufferedMessage:
    """缓冲区里的一条原始消息。"""

    event_id: str
    text: str
    sent_at: float
    kind: str = "text"
    quoted: str = ""
    is_late: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "text": self.text,
            "sent_at": self.sent_at,
            "kind": self.kind,
            "quoted": self.quoted,
            "is_late": self.is_late,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BufferedMessage":
        return cls(
            event_id=str(data.get("event_id", "")),
            text=str(data.get("text", "")),
            sent_at=float(data.get("sent_at", 0.0)),
            kind=str(data.get("kind", "text")),
            quoted=str(data.get("quoted", "")),
            is_late=bool(data.get("is_late", False)),
        )


@dataclass
class MergeGroup:
    """一个正在累积的合并组。"""

    key: MergeKey
    members: list[BufferedMessage] = field(default_factory=list)
    opened_at: float = 0.0
    last_seen: float = 0.0
    seen_events: set[str] = field(default_factory=set)
    group_id: str = ""

    def add(self, message: BufferedMessage) -> bool:
        """加入一条消息。重复的 event_id 会被忽略，返回 False。"""
        if message.event_id in self.seen_events:
            return False
        self.seen_events.add(message.event_id)
        self.members.append(message)
        self.last_seen = message.sent_at
        if not self.opened_at:
            self.opened_at = message.sent_at
        return True

    @property
    def size(self) -> int:
        return len(self.members)

    def should_flush(self, now: float, silence_window: float, max_wait: float) -> bool:
        """两条时间线任一满足就结算。"""
        if not self.members:
            return False
        if now - self.last_seen >= silence_window:
            return True
        if now - self.opened_at >= max_wait:
            return True
        return False

    def merged_text(self) -> str:
        """按时间顺序拼成一段文本。

        保留换行而不是加空格：模型要能看出这是几句话，追加、改量和取消
        在原文里就是分开的。合成为一个带空格的字符串会把它们读成一句。
        """
        return "\n".join(item.text for item in sorted(self.members, key=lambda m: m.sent_at) if item.text)


@dataclass
class MergedBatch:
    """一次结算的结果。代表消息 + 全部原始消息的证据映射。"""

    group_id: str
    key: MergeKey
    representative_event_id: str
    merged_event_ids: tuple[str, ...]
    text: str
    opened_at: float
    closed_at: float
    late_count: int = 0

    @property
    def size(self) -> int:
        return len(self.merged_event_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "key": self.key.to_dict(),
            "representative_event_id": self.representative_event_id,
            "merged_event_ids": list(self.merged_event_ids),
            "text": self.text,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "late_count": self.late_count,
        }


@dataclass
class MergeSettings:
    """合并参数。默认值只是起点，不是"适合商户"的结论。"""

    #: 静默窗口：最后一条之后安静这么久就结算。
    silence_window: float = 45.0
    #: 最大等待：第一条之后最多等这么久。
    max_wait: float = 90.0
    #: 短于这个长度的消息才参与合并，长消息通常本身就是完整订单。
    max_message_chars: int = 120
    #: 关闭合并时每条消息自成一组，行为与不合并一致。
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.silence_window <= 0:
            raise ValueError("silence_window must be positive")
        if self.max_wait <= 0:
            raise ValueError("max_wait must be positive")
        if self.max_wait < self.silence_window:
            # 上限小于静默窗口时「最多等这么久」成了空话。
            self.max_wait = self.silence_window

    def to_dict(self) -> dict[str, Any]:
        return {
            "silence_window": self.silence_window,
            "max_wait": self.max_wait,
            "max_message_chars": self.max_message_chars,
            "enabled": self.enabled,
        }


class MessageMerger:
    """把连续短消息合成一次解析。

    持久化到 SQLite，因此**进程重启后缓冲区仍在**，不会因为重启而把
    一条已经累积了一半的订单拆成两笔。
    """

    def __init__(self, path: str | Path = "merge.db", settings: MergeSettings | None = None,
                 clock: Callable[[], float] = _now, id_factory: Callable[[], str] | None = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._create_schema()
        self.settings = settings or MergeSettings()
        self._clock = clock
        self._counter = 0
        self._id = id_factory
        self._lock = threading.RLock()
        self._groups: dict[str, MergeGroup] = {}
        self._load_open_groups()

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS merge_groups(
                group_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                conversation TEXT NOT NULL,
                sender TEXT NOT NULL,
                topic TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                closed_at REAL,
                representative_event_id TEXT NOT NULL DEFAULT '',
                member_ids TEXT NOT NULL DEFAULT '[]',
                late_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS merge_groups_open
                ON merge_groups(status, conversation, sender, last_seen);
            CREATE TABLE IF NOT EXISTS merge_membership(
                event_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                role TEXT NOT NULL,
                seq INTEGER NOT NULL,
                is_late INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL DEFAULT '',
                sent_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS merge_membership_group
                ON merge_membership(group_id, seq);
            """
        )
        self.db.commit()

    # ------------------------------------------------------------ 持久化

    def _next_group_id(self) -> str:
        if self._id is not None:
            return self._id()
        with self._lock:
            self._counter += 1
            return f"mg_{int(self._clock() * 1000):x}_{self._counter}"

    def _load_open_groups(self) -> None:
        """重启后把未结算的缓冲区读回来。

        不恢复的话，一次重启就会把已经累积了一半的订单拆成两笔。
        """
        rows = self.db.execute("SELECT * FROM merge_groups WHERE status=?", (GROUP_OPEN,)).fetchall()
        for row in rows:
            key = MergeKey(row["account_id"], row["conversation"], row["sender"], row["topic"])
            group = MergeGroup(key=key, group_id=row["group_id"], opened_at=row["opened_at"], last_seen=row["last_seen"])
            members = self.db.execute(
                "SELECT event_id, is_late, text, sent_at FROM merge_membership WHERE group_id=? ORDER BY seq",
                (row["group_id"],),
            ).fetchall()
            group.seen_events = {item["event_id"] for item in members}
            for item in members:
                # The body is restored from the merge database itself, not
                # looked up elsewhere: the orders library may not even be
                # reachable at this point, and a restart must not lose text.
                group.members.append(
                    BufferedMessage(
                        event_id=item["event_id"],
                        text=str(item["text"] or ""),
                        sent_at=float(item["sent_at"] or row["last_seen"]),
                        is_late=bool(item["is_late"]),
                    )
                )
            self._groups[key.storage_key] = group

    def _load_message(self, event_id: str, fallback_sent_at: float) -> BufferedMessage:
        """从同库的消息表补回原文（若合并库被用作单一库时）。"""
        try:
            row = self.db.execute(
                "SELECT content, sent_at_epoch, kind FROM messages WHERE id=?", (event_id,)
            ).fetchone()
        except Exception:
            return BufferedMessage(event_id=event_id, text="", sent_at=fallback_sent_at)
        if row is None:
            return BufferedMessage(event_id=event_id, text="", sent_at=fallback_sent_at)
        return BufferedMessage(
            event_id=event_id,
            text=str(row["content"] or ""),
            sent_at=float(row["sent_at_epoch"] or fallback_sent_at),
            kind=str(row["kind"] or "text"),
        )

    def _persist_group(self, group: MergeGroup) -> None:
        self.db.execute(
            """
            INSERT INTO merge_groups(group_id,account_id,conversation,sender,topic,status,opened_at,last_seen)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id) DO UPDATE SET last_seen=excluded.last_seen, status=excluded.status
            """,
            (group.group_id, group.key.account_id, group.key.conversation, group.key.sender,
             group.key.topic, GROUP_OPEN, group.opened_at, group.last_seen),
        )
        self.db.execute("DELETE FROM merge_membership WHERE group_id=?", (group.group_id,))
        for index, member in enumerate(group.members):
            role = MESSAGE_REPRESENTATIVE if index == 0 else MESSAGE_MERGED
            self.db.execute(
                "INSERT OR REPLACE INTO merge_membership(event_id,group_id,role,seq,is_late,text,sent_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (member.event_id, group.group_id, role, index, int(member.is_late), member.text, member.sent_at),
            )
        self.db.commit()

    # -------------------------------------------------------------- 使用

    def offer(self, key: MergeKey, message: BufferedMessage) -> bool:
        """投进缓冲区。返回 False 表示这条已经被合并过或重复投递。"""
        if not self.settings.enabled:
            return False
        with self._lock:
            existing = self.db.execute(
                "SELECT group_id FROM merge_membership WHERE event_id=?", (message.event_id,)
            ).fetchone()
            if existing is not None:
                return False
            group = self._groups.get(key.storage_key)
            if group is None:
                group = MergeGroup(key=key, group_id=self._next_group_id())
                self._groups[key.storage_key] = group
            if group.add(message):
                self._persist_group(group)
                return True
            return False

    def is_merged(self, event_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM merge_membership WHERE event_id=?", (event_id,)).fetchone()
        return row is not None

    def representative_of(self, event_id: str) -> str:
        """这条消息属于哪一批。代表消息返回自己。"""
        row = self.db.execute(
            "SELECT group_id, role FROM merge_membership WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            return ""
        if row["role"] == MESSAGE_REPRESENTATIVE:
            return event_id
        head = self.db.execute(
            "SELECT event_id FROM merge_membership WHERE group_id=? AND role=? LIMIT 1",
            (row["group_id"], MESSAGE_REPRESENTATIVE),
        ).fetchone()
        return str(head["event_id"]) if head else ""

    def ready_groups(self) -> list[MergeGroup]:
        now = self._clock()
        with self._lock:
            return [
                group for group in self._groups.values()
                if group.should_flush(now, self.settings.silence_window, self.settings.max_wait)
            ]

    def flush(self, key: MergeKey | MergeGroup | None = None) -> list[MergedBatch]:
        """结算到期或指定的合并组。``key`` 可以是分组键或组对象。"""
        now = self._clock()
        batches: list[MergedBatch] = []
        with self._lock:
            if key is None:
                targets: list[MergeGroup] = self.ready_groups()
            elif isinstance(key, MergeGroup):
                targets = [key]
            else:
                group = self._groups.get(key.storage_key)
                targets = [group] if group is not None else []
            for group in targets:
                if group is None or not group.members:
                    continue
                batch = self._close(group, now)
                if batch is not None:
                    batches.append(batch)
        return batches

    def flush_all(self) -> list[MergedBatch]:
        """无视时间强制结算，用于关机前。"""
        with self._lock:
            keys = list(self._groups.keys())
        batches: list[MergedBatch] = []
        for storage_key in keys:
            with self._lock:
                group = self._groups.get(storage_key)
            if group is not None:
                batch = self._close(group, self._clock())
                if batch is not None:
                    batches.append(batch)
        return batches

    def _close(self, group: MergeGroup, now: float) -> MergedBatch | None:
        ordered = sorted(group.members, key=lambda m: m.sent_at)
        if not ordered:
            return None
        representative = ordered[0]
        batch = MergedBatch(
            group_id=group.group_id,
            key=group.key,
            representative_event_id=representative.event_id,
            merged_event_ids=tuple(item.event_id for item in ordered),
            text=group.merged_text(),
            opened_at=group.opened_at,
            closed_at=now,
            late_count=sum(1 for item in ordered if item.is_late),
        )
        self.db.execute(
            "UPDATE merge_groups SET status=?, closed_at=?, representative_event_id=?, member_ids=?, late_count=? WHERE group_id=?",
            (GROUP_CLOSED, now, batch.representative_event_id, json.dumps(list(batch.merged_event_ids)),
             batch.late_count, group.group_id),
        )
        for index, member in enumerate(ordered):
            role = MESSAGE_REPRESENTATIVE if index == 0 else MESSAGE_MERGED
            self.db.execute(
                "UPDATE merge_membership SET role=?, seq=?, is_late=? WHERE event_id=?",
                (role, index, int(member.is_late), member.event_id),
            )
        self.db.commit()
        self._groups.pop(group.key.storage_key, None)
        return batch

    def mark_late(self, event_id: str) -> bool:
        """把一条迟到的消息标出来（内存与库同时更新）。"""
        self.db.execute("UPDATE merge_membership SET is_late=1 WHERE event_id=?", (event_id,))
        self.db.commit()
        with self._lock:
            for group in self._groups.values():
                for member in group.members:
                    if member.event_id == event_id:
                        member.is_late = True
                        return True
        return True

    def evidence_map(self, event_id: str) -> dict[str, Any]:
        """代表消息 -> 全部原始消息的证据映射。"""
        row = self.db.execute(
            "SELECT group_id, role FROM merge_membership WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            return {"event_id": event_id, "group_id": "", "merged_event_ids": [], "role": ""}
        members = self.db.execute(
            "SELECT event_id, role, is_late FROM merge_membership WHERE group_id=? ORDER BY seq",
            (row["group_id"],),
        ).fetchall()
        return {
            "event_id": event_id,
            "group_id": str(row["group_id"]),
            "role": str(row["role"]),
            "merged_event_ids": [item["event_id"] for item in members],
            "late_event_ids": [item["event_id"] for item in members if int(item["is_late"])],
        }

    def stats(self) -> dict[str, int]:
        with self._lock:
            open_groups = len(self._groups)
        closed = self.db.execute(
            "SELECT COUNT(*) AS c FROM merge_groups WHERE status=?", (GROUP_CLOSED,)
        ).fetchone()
        merged = self.db.execute("SELECT COUNT(*) AS c FROM merge_membership").fetchone()
        return {
            "open_groups": open_groups,
            "closed_groups": int(closed["c"]),
            "merged_messages": int(merged["c"]),
        }

    def close(self) -> None:
        self.db.close()


def _placeholder(event_id: str) -> BufferedMessage:
    return BufferedMessage(event_id=event_id, text="", sent_at=0.0)


def _placeholder(event_id: str) -> BufferedMessage:
    return BufferedMessage(event_id=event_id, text="", sent_at=0.0)


__all__ = [
    "GROUP_CLOSED",
    "GROUP_OPEN",
    "MESSAGE_MERGED",
    "MESSAGE_PENDING",
    "MESSAGE_REPRESENTATIVE",
    "BufferedMessage",
    "MergeGroup",
    "MergeKey",
    "MergeSettings",
    "MergedBatch",
    "MessageMerger",
]
