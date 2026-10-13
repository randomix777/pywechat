"""实时监听接入：复用现有采集器、合并器、任务队列和 OrderPipeline。

设计约束（与前几轮一致）：

* **保持窗口和监听状态，不周期性关闭重开。** 窗口由 :class:`LiveListener`
  打开一次并持有；重连只重建采集，不重开窗口。
* **先持久化，再合并/解析。** 新消息先写进 ``AgentStore.events``，落库成功
  才进合并器；合并器结算后才提交解析。任何一步失败都停在已持久化的数据上。
* **首期只监听一个会话。** 由宿主选定，不做多路。
* **自己的消息不触发下单。** 采集器按方向过滤，这里再兜一层。

## 不承诺的事

* **不抢焦点未验证。** 打开独立聊天窗口本身会切前台；本模块不声称后台无焦点。
* **没有可靠补采能力。** 监听断线期间的消息可能丢失，缺口由
  :meth:`LiveListener.gaps` 显式报告，**不承诺不漏消息**。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .conversations import Conversation
from .merging import BufferedMessage, MergeKey, MessageMerger
from .reliability import MessageDirection, MessageEvent, MessageKind

#: 监听状态
LISTEN_STOPPED = "stopped"
LISTEN_STARTING = "starting"
LISTEN_RUNNING = "running"
LISTEN_DEGRADED = "degraded"      # 窗口失效/重连中，缺口需要人工补采
LISTEN_FAILED = "failed"          # 窗口一次都没打开过，重试只是干等，直接停


@dataclass
class ListenStats:
    """运行期事实。数字只统计**真的发生过的**事。"""

    started_at: float = 0.0
    events_seen: int = 0
    events_stored: int = 0
    events_skipped_own: int = 0
    events_skipped_duplicate: int = 0
    batches_flushed: int = 0
    orders_applied: int = 0
    reviews_raised: int = 0
    window_opens: int = 0
    window_reuses: int = 0
    reconnects: int = 0
    gap_windows: int = 0
    last_error: str = ""
    last_event_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "events_seen": self.events_seen,
            "events_stored": self.events_stored,
            "events_skipped_own": self.events_skipped_own,
            "events_skipped_duplicate": self.events_skipped_duplicate,
            "batches_flushed": self.batches_flushed,
            "orders_applied": self.orders_applied,
            "reviews_raised": self.reviews_raised,
            "window_opens": self.window_opens,
            "window_reuses": self.window_reuses,
            "reconnects": self.reconnects,
            "gap_windows": self.gap_windows,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
        }

    def describe(self) -> str:
        return (
            f"已看到 {self.events_seen} 条，入库 {self.events_stored} 条，"
            f"合并结算 {self.batches_flushed} 批，生成订单 {self.orders_applied} 单，"
            f"需核对 {self.reviews_raised} 单；窗口打开 {self.window_opens} 次，"
            f"重连 {self.reconnects} 次，缺口 {self.gap_windows} 处"
        )


class LiveListener:
    """持续监听一个会话，并把新消息接到合并与解析上。

    所有外部能力都注入（``dialog_opener`` / ``history_reader`` / ``listener`` /
    ``store`` / ``merger`` / ``pipeline``），因此可以在没有微信的机器上跑完整
    的时序验证 —— 包括"合并器是否真的会按时触发"这种只有持续运行才看得出来
    的行为。
    """

    def __init__(
        self,
        conversation: Conversation,
        store: Any,
        merger: MessageMerger,
        pipeline: Any = None,
        *,
        dialog_opener: Callable[..., Any] | None = None,
        listener: Callable[..., Any] | None = None,
        account_id: str = "default",
        group_members: Sequence[str] = (),
        poll_seconds: float = 2.0,
        clock: Callable[[], float] = time.time,
        on_event: Callable[[MessageEvent], None] | None = None,
        on_batch: Callable[[Any], None] | None = None,
        on_state: Callable[[str, str], None] | None = None,
        sink: Callable[[MessageEvent], tuple[bool, str]] | None = None,
        ui_lock: Any = None,
    ):
        self.conversation = conversation
        self.store = store
        self.merger = merger
        self.pipeline = pipeline
        self._dialog_opener = dialog_opener
        self._listener = listener
        #: 落库回调。返回 (是否新落进去, 落库后拿到的 id)。
        self._sink = sink
        #: 与读历史、发送共用的串行队列。监听每轮都在动微信界面，不能和它们并发。
        self._ui_lock = ui_lock
        self.account_id = account_id
        self.group_members = tuple(group_members or ())
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._clock = clock
        self.on_event = on_event
        self.on_batch = on_batch
        self.on_state = on_state

        self.state = LISTEN_STOPPED
        self.stats = ListenStats()
        self._dialog: Any = None
        self._window_generation = f"live-{int(self._clock())}"
        self._sequence = 0
        self._baseline: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        #: (开始, 结束) 区间。监听中断期间的窗口就是缺口。
        self.gaps: list[tuple[float, float]] = []

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self.stats = ListenStats(started_at=self._clock())
            self.state = LISTEN_STARTING
            self._thread = threading.Thread(target=self._loop, name="live-listener", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._record_gap(self._clock())
            self.state = LISTEN_STOPPED
        self._close_dialog()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _emit_state(self, state: str, detail: str = "") -> None:
        self.state = state
        if self.on_state:
            try:
                self.on_state(state, detail)
            except Exception:
                pass

    # ---------------------------------------------------------------- window

    def _ensure_dialog(self) -> Any:
        """**复用**已打开的窗口；只有真的没有或失效时才重开。

        之前的实现每轮监听都开关一次窗口，既慢又会打断用户。窗口是长期
        持有的资源，重开只发生在它真的不可用时，并且每次都记进统计。
        """
        with self._lock:
            if self._dialog is not None:
                try:
                    if hasattr(self._dialog, "exists") and not self._dialog.exists(timeout=0.1):
                        raise RuntimeError("window gone")
                except RuntimeError:
                    self._dialog = None
                    self._emit_state(LISTEN_DEGRADED, "聊天窗口已失效，准备重新打开")
                else:
                    self.stats.window_reuses += 1
                    return self._dialog
            if self._dialog_opener is None:
                from .WeChatTools import Navigator

                self._dialog_opener = Navigator.open_seperate_dialog_window
            self._dialog = self._dialog_opener(
                friend=self.conversation.name,
                select=False,
                is_maximize=False,
                window_minimize=False,
                close_weixin=False,
            )
            self.stats.window_opens += 1
            self._emit_state(LISTEN_RUNNING, "已打开聊天窗口")
            return self._dialog

    def _close_dialog(self) -> None:
        with self._lock:
            dialog = self._dialog
            self._dialog = None
        if dialog is None:
            return
        try:
            close = getattr(dialog, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    # ------------------------------------------------------------------ loop

    def _loop(self) -> None:
        first = True
        open_failures = 0
        while not self._stop.is_set():
            try:
                events = self._collect_once()
            except Exception as exc:
                with self._lock:
                    self.stats.last_error = str(exc)
                    self.stats.reconnects += 1
                    self._record_gap(self._clock())
                # 一次都没打开过窗口：开窗口本身就不行（4.1.15 起常见），
                # 后面每轮都是同样漫长的超时。3 次还打不开就停下来明说，
                # 别无限转圈；中途掉线的不算（first 已经过去），照常重连。
                if first and self.stats.window_opens == 0:
                    open_failures += 1
                    if open_failures >= 3:
                        self._emit_state(
                            LISTEN_FAILED,
                            f"打不开聊天窗口（已试 3 次）：{exc}。"
                            "请改用历史读取补消息。",
                        )
                        self._stop.set()
                        self._close_dialog()
                        break
                self._emit_state(LISTEN_DEGRADED, f"采集失败：{exc}")
                self._stop.wait(self.poll_seconds)
                continue

            with self._lock:
                if first:
                    # 启动基线：把**已经在窗口里**的消息标为已知，
                    # 否则会把历史消息当成新消息重复解析。
                    self._baseline = {getattr(item, "stable_id", None) or repr(item.runtime_id) for item in events}
                    first = False
                    continue
            for event in events:
                self._handle_event(event)
            self._drain_merger()
            self._stop.wait(self.poll_seconds)

    def _collect_once(self) -> list[MessageEvent]:
        if self._ui_lock is None:
            return self._collect_unlocked()
        with self._ui_lock:
            return self._collect_unlocked()

    def _collect_unlocked(self) -> list[MessageEvent]:
        dialog = self._ensure_dialog()
        listener = self._listener
        if listener is None:
            from .WeChatAuto import Monitor

            listener = Monitor.listen_on_chat
        payload = listener(
            dialog_window=dialog,
            duration="1s",
            groupMembers=list(self.group_members),
            close_dialog_window=False,   # 关键：不要关窗口
            account_id=self.account_id,
            return_events=True,
            window_generation=self._window_generation,
            stop_event=self._stop,
            sequence_start=self._sequence,
        ) or {}
        self._sequence += int(payload.get("count", 0) or len(payload.get("events", []) or []))
        events = payload.get("events")
        if events is not None:
            return [MessageEvent.from_dict(item) if isinstance(item, Mapping) else item for item in events]
        return []

    # ---------------------------------------------------------------- events

    def _persist(self, event: MessageEvent) -> tuple[bool, str]:
        """把一条事件落库，返回 ``(是否新落进去, 落库后拿到的 id)``。

        **必须用落库返回的 id**，不能拿采集侧的 ``stable_id`` 顶替。合并器和
        pipeline 之后都用这个 id 回查本地消息表，两套 id 空间不通用：
        商户客户端的 ``messages.id`` 与 ``AgentStore`` 的 ``event_id`` 毫无关系。
        早前这里直接 ``store.record_event(event)`` 然后拿 ``event.stable_id`` 投
        合并器，接进客户端后每一条消息都会被判成"本地没有这条消息"而静默丢弃。
        """
        if self._sink is not None:
            return self._sink(event)
        if self.store is not None and hasattr(self.store, "record_event"):
            return (bool(self.store.record_event(event)), event.stable_id or event.event_id)
        return (False, "")

    def _handle_event(self, event: MessageEvent) -> None:
        with self._lock:
            self.stats.events_seen += 1
            self.stats.last_event_at = self._clock()
        if event.direction in (MessageDirection.OUTGOING, MessageDirection.SYSTEM):
            with self._lock:
                self.stats.events_skipped_own += 1
            return
        identity = event.stable_id or repr(event.runtime_id)
        if identity in self._baseline:
            with self._lock:
                self.stats.events_skipped_duplicate += 1
            return

        # **先持久化**。落库失败就停在这里，绝不继续往下走。
        inserted, persisted_id = self._persist(event)
        if not inserted or not persisted_id:
            with self._lock:
                self.stats.events_skipped_duplicate += 1
            return
        with self._lock:
            self.stats.events_stored += 1
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

        # 持久化成功后才进合并器。
        if event.kind not in (MessageKind.TEXT, MessageKind.QUOTE, MessageKind.UNKNOWN):
            return
        text = event.content.strip()
        if not text or len(text) > self.merger.settings.max_message_chars:
            return
        key = MergeKey(
            account_id=self.account_id,
            conversation=self.conversation.name,
            sender=event.sender or "",
            topic=str((event.metadata or {}).get("quote") or ""),
        )
        self.merger.offer(
            key,
            BufferedMessage(
                event_id=persisted_id,
                text=text,
                sent_at=float(event.observed_at or self._clock()),
                kind=event.kind.value if hasattr(event.kind, "value") else str(event.kind),
            ),
        )

    def _drain_merger(self) -> None:
        """把**到期**的合并组结算掉并解析代表消息。

        这里调的是合并器自己的时间判定（`flush()` 内部比较静默窗口与最大
        等待），不是单元测试里手动喂的那条路径。
        """
        batches = self.merger.flush()
        for batch in batches:
            with self._lock:
                self.stats.batches_flushed += 1
            if self.on_batch:
                try:
                    self.on_batch(batch)
                except Exception:
                    pass
            if self.pipeline is None:
                continue
            outcome = self.pipeline.process(batch.representative_event_id, source="live")
            with self._lock:
                if outcome.order_id:
                    self.stats.orders_applied += 1
                if outcome.needs_review:
                    self.stats.reviews_raised += 1

    # ------------------------------------------------------------------ gaps

    def _record_gap(self, start: float) -> None:
        """把中断记成缺口。

        监听停掉或重连期间的窗口是**真会丢消息**的。没有可靠的补采能力时
        必须显式说出来，不能装作"不漏消息"。
        """
        if not self.gaps:
            self.gaps.append((start, start))
        else:
            begin, _ = self.gaps[-1]
            if start - begin <= 0.05:
                return
            self.gaps.append((start, start))

    def mark_gap(self, start: float, end: float) -> None:
        """外部（例如窗口被用户关掉）报告的一段缺口。"""
        with self._lock:
            self.gaps.append((float(start), float(end)))
            self.stats.gap_windows += 1

    def gap_report(self) -> list[dict[str, Any]]:
        report = []
        for start, end in self.gaps:
            report.append(
                {
                    "from": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                    "to": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
                    "seconds": round(max(0.0, end - start), 1),
                    "note": "这段时间没有采集，消息可能丢失，需要单独补采",
                }
            )
        return report

    def summary(self) -> str:
        return self.stats.describe()


__all__ = [
    "LISTEN_DEGRADED",
    "LISTEN_RUNNING",
    "LISTEN_STARTING",
    "LISTEN_STOPPED",
    "LiveListener",
    "ListenStats",
]
