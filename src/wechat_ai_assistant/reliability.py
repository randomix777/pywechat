from __future__ import annotations

import json
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from time import sleep as default_sleep
from time import time
from typing import Any, Callable, Hashable, Mapping, TypeVar
from uuid import uuid4

T = TypeVar("T")


class OperationQueueFull(RuntimeError):
    pass


class OperationQueueClosed(RuntimeError):
    pass


class MessageDirection(str, Enum):
    UNKNOWN = "unknown"
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    SYSTEM = "system"


class MessageKind(str, Enum):
    UNKNOWN = "unknown"
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"
    FILE = "file"
    LINK = "link"
    QUOTE = "quote"
    CARD = "card"
    LOCATION = "location"
    RED_PACKET = "red_packet"
    TRANSFER = "transfer"
    CALL = "call"
    HISTORY = "history"
    OTHER = "other"


def _coerce_enum(value: Any, enum_type: type[Enum], default: Enum):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MessageEvent:
    chat_id: str
    content: str
    account_id: str = "default"
    sender: str | None = None
    direction: MessageDirection | str = MessageDirection.UNKNOWN
    kind: MessageKind | str = MessageKind.UNKNOWN
    observed_at: float = field(default_factory=time)
    sequence: int | None = None
    runtime_id: Hashable | None = None
    stable_id: str | None = None
    window_generation: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    event_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "chat_id", str(self.chat_id))
        object.__setattr__(self, "content", str(self.content))
        object.__setattr__(self, "account_id", str(self.account_id))
        object.__setattr__(self, "sender", None if self.sender is None else str(self.sender))
        object.__setattr__(self, "direction", _coerce_enum(self.direction, MessageDirection, MessageDirection.UNKNOWN))
        object.__setattr__(self, "kind", _coerce_enum(self.kind, MessageKind, MessageKind.UNKNOWN))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        stable_id = self.stable_id.strip() if isinstance(self.stable_id, str) else self.stable_id
        object.__setattr__(self, "stable_id", stable_id or None)
        if isinstance(self.runtime_id, list):
            object.__setattr__(self, "runtime_id", tuple(self.runtime_id))
        object.__setattr__(self, "event_id", self.event_id or uuid4().hex)

    @property
    def normalized_content(self) -> str:
        return " ".join(self.content.split())

    @property
    def identity_quality(self) -> str:
        if self.stable_id is not None:
            return "stable"
        if self.sequence is not None and self.window_generation is not None:
            return "sequence"
        if self.runtime_id is not None and self.window_generation is not None:
            return "runtime"
        return "heuristic"

    def dedup_key(self) -> tuple[Any, ...] | None:
        base = (self.account_id, self.chat_id)
        if self.stable_id is not None:
            return base + ("stable", self.stable_id)
        if self.sequence is not None and self.window_generation is not None:
            return base + ("sequence", self.window_generation, self.sequence)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event_id": self.event_id,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "sender": self.sender,
            "content": self.content,
            "direction": self.direction.value,
            "kind": self.kind.value,
            "observed_at": self.observed_at,
            "sequence": self.sequence,
            "runtime_id": list(self.runtime_id) if isinstance(self.runtime_id, tuple) else self.runtime_id,
            "stable_id": self.stable_id,
            "window_generation": self.window_generation,
            "metadata": dict(self.metadata),
            "identity_quality": self.identity_quality,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MessageEvent":
        return cls(
            event_id=data.get("event_id"),
            account_id=data.get("account_id", "default"),
            chat_id=data.get("chat_id", ""),
            sender=data.get("sender"),
            content=data.get("content", ""),
            direction=data.get("direction", MessageDirection.UNKNOWN),
            kind=data.get("kind", MessageKind.UNKNOWN),
            observed_at=float(data.get("observed_at", time())),
            sequence=data.get("sequence"),
            runtime_id=data.get("runtime_id"),
            stable_id=data.get("stable_id"),
            window_generation=data.get("window_generation"),
            metadata=data.get("metadata", {}),
        )


class MessageLedger:
    def __init__(self, max_entries: int = 2048, ttl_seconds: float = 900, clock: Callable[[], float] = monotonic):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds cannot be negative")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[tuple[Any, ...], float] = OrderedDict()
        self._lock = Lock()

    def accept(self, event: MessageEvent) -> bool:
        key = event.dedup_key()
        if key is None:
            return True
        with self._lock:
            now = self._clock()
            self._purge(now)
            if key in self._entries:
                self._entries[key] = now
                self._entries.move_to_end(key)
                return False
            self._entries[key] = now
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return True

    def filter(self, events: list[MessageEvent]) -> list[MessageEvent]:
        return [event for event in events if self.accept(event)]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            now = self._clock()
            return [
                {"key": list(key), "age": max(0.0, now - seen_at)}
                for key, seen_at in self._entries.items()
            ]

    def restore(self, entries: list[Mapping[str, Any]]) -> None:
        with self._lock:
            now = self._clock()
            restored: dict[tuple[Any, ...], float] = {}
            for entry in entries:
                key = entry.get("key")
                if not isinstance(key, list):
                    continue
                try:
                    if "age" in entry:
                        seen_at = now - max(0.0, float(entry["age"]))
                    else:
                        seen_at = float(entry["seen_at"])
                except (KeyError, TypeError, ValueError):
                    continue
                restored[tuple(key)] = seen_at
            self._entries.clear()
            for key, seen_at in sorted(restored.items(), key=lambda item: item[1]):
                self._entries[key] = seen_at
            self._purge(self._clock())
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
        payload = {"version": 2, "ttl_seconds": self.ttl_seconds, "entries": self.snapshot()}
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def load(cls, path: str | Path, max_entries: int = 2048, ttl_seconds: float | None = None, clock: Callable[[], float] = monotonic) -> "MessageLedger":
        source = Path(path)
        payload = {}
        if source.exists():
            payload = json.loads(source.read_text(encoding="utf-8"))
        if ttl_seconds is None:
            ttl_seconds = float(payload.get("ttl_seconds", 900))
        ledger = cls(max_entries=max_entries, ttl_seconds=ttl_seconds, clock=clock)
        ledger.restore(payload.get("entries", []))
        return ledger

    def _purge(self, now: float) -> None:
        if self.ttl_seconds == 0:
            self._entries.clear()
            return
        cutoff = now - self.ttl_seconds
        while self._entries:
            _, seen_at = next(iter(self._entries.items()))
            if seen_at >= cutoff:
                break
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class UiOperationQueue:
    def __init__(self, max_pending: int = 100, thread_name: str = "wechat-ai-assistant-ui"):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._queue: Queue[tuple[Future, Callable[..., T], tuple[Any, ...], dict[str, Any]] | None] = Queue(maxsize=max_pending)
        self._state_lock = Lock()
        self._stop_event = Event()
        self._close_complete = Event()
        self._thread = Thread(target=self._run, name=thread_name, daemon=True)
        self._closed = False
        self._running = False
        self._thread.start()

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def busy(self) -> bool:
        """当前**正在执行**某个操作。

        和 :attr:`pending` 不一样：``pending`` 只数排队的，正在跑的那一个
        已经出队了。读历史一次可能跑几分钟，这段时间里别的线程
        （比如每几秒查一次微信状态的监视器）必须知道**别去碰微信窗口** ——
        两个线程同时驱动同一个窗口，读出来的是残缺甚至空的结果。
        """
        with self._state_lock:
            return self._running or not self._queue.empty()

    def submit(self, operation: Callable[..., T], *args: Any, **kwargs: Any) -> Future:
        if not callable(operation):
            raise TypeError("operation must be callable")
        future: Future = Future()
        item = (future, operation, args, kwargs)
        with self._state_lock:
            if self._closed:
                raise OperationQueueClosed("UI operation queue is closed")
            try:
                self._queue.put_nowait(item)
            except Full as exc:
                raise OperationQueueFull("UI operation queue is full") from exc
        return future

    def close(self, wait: bool = True, timeout: float | None = None) -> None:
        with self._state_lock:
            if not self._closed:
                self._closed = True
                self._stop_event.set()
        if not wait or current_thread() is self._thread:
            return
        self._close_complete.wait(timeout)
        if timeout is not None and not self._close_complete.is_set():
            raise TimeoutError("UI operation queue did not stop before timeout")

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set() or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=0.05)
                except Empty:
                    continue
                if item is None:
                    self._queue.task_done()
                    break
                future, operation, args, kwargs = item
                try:
                    if not future.set_running_or_notify_cancel():
                        continue
                    with self._state_lock:
                        self._running = True
                    try:
                        result = operation(*args, **kwargs)
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
                except BaseException as exc:
                    if not future.done():
                        future.set_exception(exc)
                finally:
                    with self._state_lock:
                        self._running = False
                    self._queue.task_done()
        except BaseException as exc:
            self._fail_pending(exc)
        finally:
            self._close_complete.set()

    def _fail_pending(self, error: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            try:
                if item is not None:
                    future = item[0]
                    if not future.done():
                        future.set_exception(error)
            finally:
                self._queue.task_done()

    def __enter__(self) -> "UiOperationQueue":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def retry_call(
    operation: Callable[[], T],
    attempts: int = 3,
    delay: float = 0.1,
    backoff: float = 2.0,
    max_delay: float = 2.0,
    retryable: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], None] = default_sleep,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if delay < 0 or max_delay < 0 or backoff < 1:
        raise ValueError("invalid retry timing")
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if attempt == attempts - 1 or (retryable is not None and not retryable(exc)):
                raise
            sleep(min(max_delay, delay * (backoff ** attempt)))
    raise RuntimeError("retry loop exited unexpectedly")
