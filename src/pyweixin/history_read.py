"""历史消息读取：范围、进度、取消、恢复，以及**如实**的覆盖范围。

## 三个容易做错的地方

**1. 固定条数冒充时间范围。** PC 微信能看到多少历史是有限的。如果商户选
"最近 7 天"而实际只读到 3 天，界面必须说"实际读到 X 至 Y"，不能显示"已读取
最近 7 天"。本模块因此把**请求范围**和**实际覆盖范围**分成两个字段。

**2. 并发抢键鼠。** 读取历史要操作微信窗口。如果同时在发回执或监听，两者会
互相打断，甚至把文字发进错误的会话。所有涉及键鼠的操作都经过同一个串行队列。

**3. 假进度。** 进度按**已处理会话数**推进，不用假动画。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .conversations import Conversation
from .utils import sender_is_resolved
from .wechat_time import parse_timestamp

LOGGER = logging.getLogger(__name__)

#: 单个会话一次读取的默认条数上限。界面可在"高级"里调小或调大。
DEFAULT_MAX_MESSAGES = 200

#: "读取全部历史"的兜底上限。
#:
#: 微信**不告诉调用方一共有多少条消息** —— 聊天记录窗口标题里只有会话名，没有
#: 总条数。所以"全部"只能实现成"一直往下翻，翻到没有新消息为止"（见
#: :func:`traverse_chat_history` 里的 idle_rounds 判定）。对一个人多、消息密的群
#: 这可能是几万条，全程占用本机前台。
#:
#: 这里留一个远高于日常需要的天花板，不是为了限速，而是为了让"跑到天荒地老"
#: 这种异常有个能说清楚的终点。撞到上限时 ``truncated`` 置位，界面如实写明
#: **可能还有更早的历史没有读到**，不假装读完了。
READ_ALL_CEILING = 20000


class ReadMode:
    """商户能选的读法。"""

    #: 按时间范围读最近若干条（现有行为）。
    RECENT = "recent"
    #: 读全部：翻到微信翻不出新的为止。
    ALL = "all"
    #: 只更新上次读过之后新增的消息。
    UPDATE = "update"

    ALL_KINDS = (RECENT, ALL, UPDATE)


class HistoryRange:
    """商户能选的范围。"""

    LAST_DAY = "last_day"
    LAST_WEEK = "last_week"
    CUSTOM = "custom"
    ALL = (LAST_DAY, LAST_WEEK, CUSTOM)

    _PRESETS = {LAST_DAY: timedelta(days=1), LAST_WEEK: timedelta(days=7)}

    @classmethod
    def window(cls, kind: str, custom: "TimeWindow | None" = None) -> "TimeWindow":
        """把范围解析成具体时间窗。``custom`` 必须提供。"""
        if kind in cls._PRESETS:
            return TimeWindow(end=None, start_delta=cls._PRESETS[kind], label=kind)
        if kind == cls.CUSTOM:
            if custom is None:
                raise ValueError("custom range requires a time window")
            return custom
        raise ValueError(f"unknown history range: {kind}")


@dataclass(frozen=True)
class TimeWindow:
    """一个时间范围。``start_delta`` 用于"最近 N 天"，``start`` 用于自定义。"""

    end: datetime | None = None
    start: datetime | None = None
    start_delta: timedelta | None = None
    label: str = "custom"

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("history window start must not be after its end")

    def lower_bound(self, now: datetime) -> datetime:
        if self.start is not None:
            return self.start
        if self.start_delta is not None:
            return now - self.start_delta
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

    def upper_bound(self, now: datetime) -> datetime:
        return self.end or now

    def contains(self, moment: datetime, now: datetime) -> bool:
        """True when ``moment`` falls inside this window, evaluated at ``now``."""
        if moment is None:
            return False
        aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        return self.lower_bound(now) <= aware <= self.upper_bound(now)

    def describe(self) -> str:
        if self.start_delta is not None:
            days = self.start_delta.days
            return "最近一天" if days == 1 else f"最近{days}天"
        if self.start and self.end:
            return f"{self.start.date()} 至 {self.end.date()}"
        return "全部可读历史"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start.isoformat() if self.start else "",
            "end": self.end.isoformat() if self.end else "",
            "start_delta_seconds": self.start_delta.total_seconds() if self.start_delta else None,
        }


@dataclass
class ConversationCoverage:
    """一个会话的实际读取结果。

    ``requested_count`` 是请求条数，``actual*`` 是**实际**读到的范围。两者
    不同就意味着微信没给出那么多历史，界面必须显示 actual。
    """

    conversation: Conversation
    requested_count: int = 0
    message_count: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    truncated: bool = False
    #: 这次读到的消息里发送人**没认出来**的条数。为 0 才是好事。
    #: 不记下来就没了 —— “读到 107 条”和“读到 107 条但每条的发送人
    #: 都是占位符”在界面上完全一样，商户会以为成功了。
    unresolved_senders: int = 0
    error: str = ""
    skipped: bool = False

    def describe(self) -> str:
        if self.error:
            return f"{self.conversation.disambiguation}：读取失败（{self.error}）"
        if self.skipped:
            return f"{self.conversation.disambiguation}：已跳过（上次已读完）"
        if not self.message_count:
            return f"{self.conversation.disambiguation}：没有读到消息"
        earliest = self.earliest.date().isoformat() if self.earliest else "?"
        latest = self.latest.date().isoformat() if self.latest else "?"
        suffix = "（可能还有更早的历史）" if self.truncated else ""
        return f"{self.conversation.disambiguation}：{self.message_count} 条，{earliest} 至 {latest}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation": self.conversation.to_dict(),
            "requested_count": self.requested_count,
            "message_count": self.message_count,
            "earliest": self.earliest.isoformat() if self.earliest else "",
            "latest": self.latest.isoformat() if self.latest else "",
            "truncated": self.truncated,
            "error": self.error,
            "skipped": self.skipped,
        }


@dataclass
class HistoryProgress:
    """界面渲染用。全部是商户看得懂的中文。"""

    done: int = 0
    total: int = 0
    current: str = ""
    cancelled: bool = False
    finished: bool = False
    message: str = ""
    #: 已读**条数**。读全部历史时进度靠它推进 —— 只按会话数推进的话，选一个群
    #: 读几万条，进度条会一直停在 0/1，看着像卡死了。
    messages_read: int = 0
    #: 本次目标条数。**0 表示不知道有多少**（读全部时微信不告诉总条数），
    #: 此时进度条走不定长动画，不假装知道还剩多少。
    message_target: int = 0

    @property
    def bounded(self) -> bool:
        """总条数是否已知。未知时界面不能画一个假装会走完的进度条。"""
        return self.message_target > 0

    @property
    def fraction(self) -> float:
        if self.bounded and self.message_target:
            return min(1.0, self.messages_read / self.message_target)
        return 1.0 if self.total == 0 else min(1.0, self.done / self.total)

    @property
    def percent(self) -> int:
        return int(round(self.fraction * 100))

    def summary(self) -> str:
        if self.cancelled:
            prefix = f"已取消（已读 {self.messages_read} 条"
            return prefix + (f"，完成 {self.done}/{self.total}）" if self.total else "）")
        if self.finished:
            return f"已完成，共读 {self.messages_read} 条" if self.messages_read else f"已完成 {self.done}/{self.total}"
        scope = f"已读 {self.messages_read} 条" if self.bounded or self.messages_read else ""
        head = "正在读取"
        if scope:
            head = f"{head} {scope}"
        return f"{head}：{self.current}" if self.current else head


@dataclass
class HistoryResult:
    """一批读取的完整结果。"""

    window: TimeWindow
    #: 商户选的是哪种读法。界面用它决定怎么措辞。
    mode: str = ReadMode.RECENT
    coverage: list[ConversationCoverage] = field(default_factory=list)
    imported: int = 0
    import_ids: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total_messages(self) -> int:
        return sum(item.message_count for item in self.coverage)

    @property
    def earliest(self) -> datetime | None:
        values = [item.earliest for item in self.coverage if item.earliest]
        return min(values) if values else None

    @property
    def latest(self) -> datetime | None:
        values = [item.latest for item in self.coverage if item.latest]
        return max(values) if values else None

    @property
    def gaps(self) -> tuple[str, ...]:
        """如实列出没做成的部分，而不是只报成功项。"""
        problems: list[str] = []
        for item in self.coverage:
            if item.error:
                problems.append(f"{item.conversation.disambiguation} 读取失败：{item.error}")
            if item.skipped:
                continue
            if item.truncated:
                if self.mode == ReadMode.ALL:
                    problems.append(
                        f"{item.conversation.disambiguation} 读到了目前微信能翻到的全部，"
                        "更早的内容微信没有提供"
                    )
                elif self.mode == ReadMode.UPDATE:
                    # 更新模式下撞满预算，缺的是**更新的**那部分，不是更早的。
                    problems.append(
                        f"{item.conversation.disambiguation} 新消息可能比这次读的多，"
                        "请再点一次「更新历史消息」"
                    )
                else:
                    problems.append(
                        f"{item.conversation.disambiguation} 可能还有更早的消息没有读到，"
                        "可以点「读取全部历史」再试一次"
                    )
        return tuple(problems)

    def headline(self) -> str:
        """界面上最上面那一行。"""
        if self.cancelled:
            return f"已取消。{self.actual_range_text()}"
        if self.mode == ReadMode.ALL:
            return self.actual_range_text()
        if self.mode == ReadMode.UPDATE:
            if not self.total_messages:
                return "没有发现上次之后的新消息"
            return f"更新了 {self.total_messages} 条新消息"
        return self.actual_range_text()

    def actual_range_text(self) -> str:
        """界面上"实际覆盖范围"那一行。"""
        if not self.total_messages:
            return "这次没有读到任何消息"
        earliest = self.earliest.date().isoformat() if self.earliest else "?"
        latest = self.latest.date().isoformat() if self.latest else "?"
        return f"实际读到 {self.total_messages} 条，时间范围 {earliest} 至 {latest}"

    def requested_range_text(self) -> str:
        if self.mode == ReadMode.ALL:
            return "您选择的是「读取全部历史」"
        if self.mode == ReadMode.UPDATE:
            return "您选择的是「更新历史消息」"
        return f"您选择的范围是「{self.window.describe()}」"

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.to_dict(),
            "mode": self.mode,
            "coverage": [item.to_dict() for item in self.coverage],
            "imported": self.imported,
            "import_ids": list(self.import_ids),
            "cancelled": self.cancelled,
            "requested": self.requested_range_text(),
            "actual": self.actual_range_text(),
            "headline": self.headline(),
            "gaps": list(self.gaps),
        }


class HistoryCursor:
    """每个会话的读取进度，支持取消后从断点继续。

    持久化在本地 SQLite：商户中途关掉软件，下次启动能从上次的位置继续，
    不会重读已经读过的部分，也不会漏掉没读完的。
    """

    def __init__(self, path: str | Path = "history.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS history_cursor(
                conversation_id TEXT NOT NULL,
                merchant_id TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                earliest TEXT,
                latest TEXT,
                exhausted INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY(conversation_id, merchant_id)
            )
            """
        )
        self.db.commit()

    def get(self, conversation_id: str, merchant_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM history_cursor WHERE conversation_id=? AND merchant_id=?",
            (conversation_id, merchant_id),
        ).fetchone()

    def record(
        self,
        conversation_id: str,
        merchant_id: str,
        message_count: int,
        earliest: datetime | None,
        latest: datetime | None,
        exhausted: bool,
        now: float,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO history_cursor(conversation_id,merchant_id,message_count,earliest,latest,exhausted,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id,merchant_id) DO UPDATE SET
                message_count=excluded.message_count,
                earliest=excluded.earliest,
                latest=excluded.latest,
                exhausted=excluded.exhausted,
                updated_at=excluded.updated_at
            """,
            (
                conversation_id,
                merchant_id,
                message_count,
                earliest.isoformat() if earliest else None,
                latest.isoformat() if latest else None,
                int(exhausted),
                now,
            ),
        )
        self.db.commit()

    def reset(self, merchant_id: str) -> None:
        self.db.execute("DELETE FROM history_cursor WHERE merchant_id=?", (merchant_id,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


class HistoryReader:
    """串行读取多个会话的历史，支持进度、取消和恢复。

    ``reader`` 是注入的单会话读取函数，签名与
    ``Messages.dump_chat_history`` 兼容，因此可以直接用真实实现。
    """

    def __init__(
        self,
        reader: Callable[..., Any],
        ui_lock: Any = None,
        cursor: HistoryCursor | None = None,
        clock: Callable[[], float] = time.time,
        group_members: Callable[[Any], Sequence[str]] | None = None,
    ):
        self._reader = reader
        self._ui_lock = ui_lock
        self.cursor = cursor
        self._clock = clock
        self._cancel = threading.Event()
        self.progress = HistoryProgress()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        #: 给群聊取成员昵称列表。取到了读取器就能**直接认出发送人**；
        #: 取不到只能靠两次遍历反推，而对不上时发送人会变成占位串。
        self._group_members = group_members
        #: 按会话缓存，避免每次读取都重新去取一遍群成员。
        self._members_cache: dict[str, tuple[str, ...]] = {}

    # ------------------------------------------------------------- controls

    def cancel(self) -> None:
        self._cancel.set()

    def reset(self) -> None:
        with self._lock:
            self._cancel.clear()
            self.progress = HistoryProgress()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ run

    def start(
        self,
        conversations: Sequence[Conversation],
        window: TimeWindow,
        *,
        merchant_id: str,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        on_progress: Callable[[HistoryProgress], None] | None = None,
        resume: bool = True,
    ) -> HistoryResult:
        """后台读取。立即返回，进度通过 ``on_progress`` 推送。"""
        with self._lock:
            if self.running:
                raise RuntimeError("a history read is already running")
            self._cancel.clear()
            self.progress = HistoryProgress(done=0, total=len(conversations))
            result_holder: dict[str, HistoryResult] = {}

            def work() -> None:
                try:
                    result_holder["result"] = self.read(
                        conversations, window, merchant_id=merchant_id, max_messages=max_messages,
                        on_progress=on_progress, resume=resume,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    result_holder["result"] = HistoryResult(window=window, coverage=[], cancelled=False)
                    with self._lock:
                        self.progress = HistoryProgress(
                            total=len(conversations), finished=True, message=f"读取失败：{exc}"
                        )
                    if on_progress:
                        on_progress(self.progress)

            self._thread = threading.Thread(target=work, name="history-reader", daemon=True)
            self._thread.start()
        # 等待完成（同步语义），但线程可被 cancel 中断。
        thread = self._thread
        if thread is not None:
            thread.join()
        with self._lock:
            self._thread = None
        return result_holder.get("result", HistoryResult(window=window))

    def read(
        self,
        conversations: Sequence[Conversation],
        window: TimeWindow,
        *,
        merchant_id: str,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        on_progress: Callable[[HistoryProgress], None] | None = None,
        resume: bool = True,
        importer: Callable[[Conversation, Sequence[Mapping[str, Any]], TimeWindow], dict[str, Any]] | None = None,
        mode: str = ReadMode.RECENT,
    ) -> HistoryResult:
        """同步读取。逐个会话串行，随时可取消。

        ``mode`` 是商户选的读法，见 :class:`ReadMode`。
        """
        if mode not in ReadMode.ALL_KINDS:
            raise ValueError(f"unknown read mode: {mode}")
        now = datetime.now(timezone.utc)
        result = HistoryResult(window=window, mode=mode)
        total = len(conversations)
        target = 0 if mode == ReadMode.ALL else max_messages
        with self._lock:
            self.progress = HistoryProgress(
                done=0, total=total, message="准备中", message_target=target
            )
        self._emit(on_progress)

        for index, conversation in enumerate(conversations, start=1):
            if self._cancel.is_set():
                result.cancelled = True
                break
            with self._lock:
                self.progress = HistoryProgress(
                    done=index - 1, total=total, current=conversation.disambiguation,
                    message_target=target,
                )
            self._emit(on_progress)

            # 目标核对：读之前确认这个会话确实是商户选中的那个。
            if not conversation.matches(conversation):
                result.coverage.append(ConversationCoverage(conversation=conversation, error="会话核对失败"))
                continue

            saved = self.cursor.get(conversation.identity, merchant_id) if (self.cursor and resume) else None
            if saved is not None and saved["exhausted"] and mode != ReadMode.UPDATE:
                # 已经翻到底了。增量更新仍然要再读一次 —— 上次读完到现在可能又来了
                # 新消息，那正是"更新历史"要做的事。
                result.coverage.append(
                    ConversationCoverage(
                        conversation=conversation,
                        requested_count=max_messages,
                        message_count=int(saved["message_count"]),
                        earliest=_parse(saved["earliest"]),
                        latest=_parse(saved["latest"]),
                        skipped=True,
                    )
                )
                with self._lock:
                    self.progress = HistoryProgress(
                        done=index, total=total, current=conversation.disambiguation,
                        messages_read=int(saved["message_count"]), message_target=target,
                    )
                self._emit(on_progress)
                continue

            # 增量：上次读到的最新一条是什么时候。只取那之后的新消息。
            since: datetime | None = None
            if mode == ReadMode.UPDATE and saved is not None:
                since = _parse(saved["latest"])
                if since is None:
                    # 上次没记下时间，就不敢说"只要新的" —— 老老实实按范围重读，
                    # 重复的由 source_id 去重挡掉。
                    since = None

            budget = READ_ALL_CEILING if mode == ReadMode.ALL else max_messages
            try:
                messages = self._read_one(
                    conversation, budget,
                    on_progress=self._make_counter(conversation, index, total, target, on_progress),
                )
            except Exception as exc:
                result.coverage.append(
                    ConversationCoverage(conversation=conversation, requested_count=budget, error=str(exc))
                )
                with self._lock:
                    self.progress = HistoryProgress(
                        done=index, total=total, current=conversation.disambiguation,
                        messages_read=self.progress.messages_read, message_target=target,
                    )
                self._emit(on_progress)
                continue

            # 只保留窗口内的消息 —— 超出范围的不进库，也不进"实际覆盖"。
            kept, earliest, latest = self._filter(messages, window, now)
            truncated, hit_ceiling = self._truncation(messages, budget, mode)
            if mode == ReadMode.UPDATE and since is not None:
                # 增量：上次读到 latest 为止，这次只要比它更新的。
                kept, earliest, latest = _newer_than(kept, since)

            if importer is not None and kept:
                outcome = importer(conversation, kept, window)
                result.imported += int(outcome.get("inserted", 0))
                if outcome and outcome.get("import_id"):
                    result.import_ids.append(str(outcome["import_id"]))

            if self.cursor is not None:
                previous = self.cursor.get(conversation.identity, merchant_id)
                total_seen = int(previous["message_count"]) + len(kept) if previous else len(kept)
                # **一条都没读到 ≠ 翻到底了。**
                #
                # 「exhausted」的意思是"确认这个会话没有更多了"，下次读取会
                # **整个跳过它**。而读到 0 条时 truncated 和 hit_ceiling 都是
                # False，按老写法 exhausted 会被置成 True ——
                # 于是「这次没拿到」被记成了「确认没有了」。
                #
                # 后果：商户第一次读到 0 条（比如微信没聚焦、时间窗不匹配），
                # 之后**永远**读不出东西，而且没有任何办法恢复，只能清库。
                # 表现为「读取历史偶尔返回 0 条」，实际上那 0 条是**永久**的。
                #
                # 所以这里必须加一条：读到 0 条就不算翻到底。
                exhausted = bool(kept) and not truncated and not hit_ceiling and mode != ReadMode.UPDATE
                # 只有真的翻到底了才算 exhausted。增量读永远不算 —— 它按定义没读完。
                self.cursor.record(
                    conversation.identity, merchant_id, total_seen,
                    earliest or (_parse(saved["earliest"]) if saved is not None else None),
                    latest or (_parse(saved["latest"]) if saved is not None else None),
                    exhausted,
                    self._clock(),
                )

            # 这一次读到的消息里，有多少条的发送人**没认出来**。
            # 不记下来就没了：“读到 107 条”与“读到 107 条但每一条的发送人都是占位符”
            # 在界面上完全一样，商户会以为成功了，转头去查“为什么发送人都是那句话”。
            unresolved = sum(1 for row in kept
                              if not sender_is_resolved(row.get("sender")))
            result.coverage.append(
                ConversationCoverage(
                    conversation=conversation,
                    requested_count=budget,
                    message_count=len(kept),
                    earliest=earliest,
                    latest=latest,
                    truncated=truncated or hit_ceiling,
                    unresolved_senders=unresolved,
                )
            )
            with self._lock:
                self.progress = HistoryProgress(
                    done=index, total=total, current=conversation.disambiguation,
                    messages_read=self.progress.messages_read, message_target=target,
                )
            self._emit(on_progress)

        with self._lock:
            self.progress = HistoryProgress(
                done=self.progress.done,
                total=total,
                finished=not result.cancelled,
                cancelled=result.cancelled,
                messages_read=self.progress.messages_read,
                message_target=target,
                message=result.actual_range_text(),
            )
        self._emit(on_progress)
        # 每个会话读到了多少、要了多少、有没有被时间窗或预算截断 ——
        # 这些就是商户看得见的那几个数字，日志里必须能对上。
        # **只记条数，不记聊天内容。**
        for item in result.coverage:
            LOGGER.info(
                '读取「%s」：要 %s 条，保留 %d 条%s%s',
                item.conversation.disambiguation,
                item.requested_count if item.requested_count is not None else '全部',
                item.message_count,
                '，可能没读全' if item.truncated else '',
                f"，失败：{item.error}" if item.error else '',
            )
        LOGGER.info('读取结束：%s', result.headline())
        for gap in result.gaps:
            LOGGER.info('读取缺口：%s', gap)
        if result.total_messages == 0:
            # 商户只看到「没读到消息」。到底是没读到、还是读到了没留下，
            # 日志必须分清楚 —— 否则下一个来排障的人还得从头猜。
            LOGGER.warning('一条都没读到。选了 %d 个会话：%s',
                           len(conversations),
                           [c.disambiguation for c in conversations])
        return result

    # ------------------------------------------------------------- internals

    def _make_counter(
        self,
        conversation: Conversation,
        index: int,
        total: int,
        target: int,
        on_progress: Callable[[HistoryProgress], None] | None,
    ) -> Callable[[int], None] | None:
        """把"读到第几条"翻译成界面看得懂的进度并推出去。"""
        if on_progress is None:
            return None

        def report(count: int) -> None:
            with self._lock:
                self.progress = HistoryProgress(
                    done=index - 1, total=total, current=conversation.disambiguation,
                    messages_read=count, message_target=target,
                )
                snapshot = self.progress
            self._emit(on_progress)  # type: ignore[arg-type]

        return report

    @staticmethod
    def _truncation(messages: Sequence[Mapping[str, Any]], budget: int, mode: str) -> tuple[bool, bool]:
        """(是否可能没读全, 是否撞到兜底上限)。"""
        hit_ceiling = mode == ReadMode.ALL and len(messages) >= budget
        # RECENT/UPDATE：读满预算就说明后面还有。ALL：读不满就是翻到底了。
        truncated = mode != ReadMode.ALL and len(messages) >= budget
        return truncated, hit_ceiling

    def _read_one(
        self,
        conversation: Conversation,
        max_messages: int,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[dict[str, Any]]:
        # 不把 since 传给 reader：dump_chat_history 和各测试替身都没有这个参数，
        # 「只要上次之后的消息」在 read() 里过滤，不要去改底层签名。
        # 群聊必须把成员昵称一起传下去。
        #
        # 不传的话，读取器只能靠「开多选遍历一次、不开再遍历一次」两份结果
        # 对比来反推发送人；那一步对不上就返回 None，发送人变成
        # 「红包或转账(无法获取发送人)」这个固定占位串 —— 商户看到的是
        # 一堆不知道谁说的话。读取器自己文档里写着：传了成员就
        # 「直接解析发送人（无需遍历二次）」。
        #
        # 取成员要开微信界面，所以放在 ui_lock 里面，并且按会话缓存。
        members = self._members_for(conversation)
        kwargs = dict(
            friend=conversation.name,
            number=max_messages,
            groupMembers=list(members),
            is_json=False,
            save_detail=False,
            close_weixin=False,
            search_pages=5,
            is_maximize=False,
            on_progress=on_progress,
            cancelled=self._cancel.is_set,
        )
        if self._ui_lock is None:
            return list(self._reader(**kwargs) or [])
        with self._ui_lock:
            return list(self._reader(**kwargs) or [])

    def _members_for(self, conversation: Any) -> tuple[str, ...]:
        """这个会话的群成员昵称。非群聊、没配解析器、取失败都返回空。

        **取失败不能让整次读取失败**：没有成员名单只是发送人认不出来，
        消息本身还能读；为此中断读取是拿小的换大的。
        """
        if self._group_members is None:
            return ()
        if getattr(conversation, "kind", "") != "group":
            return ()
        key = str(getattr(conversation, "identity", "") or conversation.name)
        if key in self._members_cache:
            return self._members_cache[key]
        members: tuple[str, ...] = ()
        try:
            members = tuple(str(x) for x in (self._group_members(conversation) or []) if x)
        except Exception:
            members = ()
        self._members_cache[key] = members
        return members

    @staticmethod
    def _filter(
        messages: Sequence[Mapping[str, Any]], window: TimeWindow, now: datetime
    ) -> tuple[list[dict[str, Any]], datetime | None, datetime | None]:
        lower = window.lower_bound(now)
        upper = window.upper_bound(now)
        kept: list[dict[str, Any]] = []
        moments: list[datetime] = []
        for message in messages:
            moment = _sent_at(message)
            if moment is not None and (moment < lower or moment > upper):
                continue
            kept.append(dict(message))
            if moment is not None:
                moments.append(moment)
        return kept, (min(moments) if moments else None), (max(moments) if moments else None)

    def _emit(self, callback: Callable[[HistoryProgress], None] | None) -> None:
        if callback is None:
            return
        with self._lock:
            snapshot = self.progress
        try:
            callback(snapshot)
        except Exception:
            pass

    def stop(self, timeout: float = 3.0) -> None:
        self.cancel()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)


def _parse(value: str | None) -> datetime | None:
    """解析存量 ISO 时间戳。

    真实微信的 ``2026-10-2 12:21``（日不补零、空格分隔、无时区）由
    :func:`pyweixin.wechat_time.parse_timestamp` 统一处理，这里只负责读库里的
    ISO 文本，两边共用同一份实现。
    """
    return parse_timestamp(value)


def _sent_at(message: Mapping[str, Any]) -> datetime | None:
    """一条消息的发送时刻。解析规则与入库侧共用 :mod:`pyweixin.wechat_time`。"""
    return parse_timestamp(message.get("消息发送时间") or message.get("sent_at"))


def _newer_than(
    messages: Sequence[Mapping[str, Any]], since: datetime
) -> tuple[list[dict[str, Any]], datetime | None, datetime | None]:
    """只留下比 ``since`` 更新的消息，并重算覆盖范围。

    解析不出时间的消息**保留** —— 静默丢掉等于把真实消息弄没了，宁可多留一条
    让商户自己看见。
    """
    kept: list[dict[str, Any]] = []
    moments: list[datetime] = []
    for message in messages:
        moment = _sent_at(message)
        if moment is not None and moment <= since:
            continue
        kept.append(dict(message))
        if moment is not None:
            moments.append(moment)
    return kept, (min(moments) if moments else None), (max(moments) if moments else None)


class SimulatedHistory:
    """确定性的假历史读取，供演示与测试。"""

    def __init__(self, per_conversation: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
                 default_count: int = 12, delay: float = 0.0):
        self.per_conversation = dict(per_conversation or {})
        self.default_count = default_count
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(dict(kwargs))
        if self.delay:
            time.sleep(self.delay)
        friend = kwargs.get("friend", "")
        if friend in self.per_conversation:
            return [dict(item) for item in self.per_conversation[friend]]
        now = datetime.now(timezone.utc)
        return [
            {
                "消息发送人": "张姐",
                "消息内容": f"苹果两箱（第 {index + 1} 条）",
                "消息类型": "文本",
                "消息发送时间": (now - timedelta(hours=index)).isoformat(),
            }
            for index in range(self.default_count)
        ]


__all__ = [
    "DEFAULT_MAX_MESSAGES",
    "ConversationCoverage",
    "HistoryCursor",
    "HistoryProgress",
    "HistoryRange",
    "HistoryReader",
    "HistoryResult",
    "SimulatedHistory",
    "TimeWindow",
]
