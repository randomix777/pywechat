from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from .reliability import MessageEvent, UiOperationQueue


_UI_OPERATION_LOCK = threading.RLock()


@dataclass(frozen=True)
class AgentCommand:
    command_id: str
    action: str
    target: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_attempts: int = 3
    retryable: bool = False
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.command_id or not self.action or not self.target:
            raise ValueError("command_id, action and target are required")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        object.__setattr__(self, "payload", dict(self.payload or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "action": self.action,
            "target": self.target,
            "payload": dict(self.payload),
            "max_attempts": self.max_attempts,
            "retryable": self.retryable,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentCommand":
        return cls(
            command_id=str(data["command_id"]),
            action=str(data["action"]),
            target=str(data["target"]),
            payload=data.get("payload", {}),
            max_attempts=int(data.get("max_attempts", 3)),
            retryable=bool(data.get("retryable", False)),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass(frozen=True)
class OutboxLease:
    command: AgentCommand
    token: str
    attempt: int


@dataclass(frozen=True)
class AgentConfig:
    group: str
    db_path: str = "agent.db"
    account_id: str = "default"
    agent_id: str = "default"
    history_limit: int = 50
    bootstrap_history: bool = True
    listen_duration: str = "30s"
    poll_interval: float = 5.0
    group_members: tuple[str, ...] = ()
    background: bool = False
    window_generation: str | None = None
    allowed_targets: tuple[str, ...] = ()
    cloud_url: str = ""
    device_token: str = ""

    def __post_init__(self):
        if not self.group:
            raise ValueError("group is required")
        if self.history_limit < 0:
            raise ValueError("history_limit cannot be negative")
        if self.poll_interval < 0:
            raise ValueError("poll_interval cannot be negative")
        object.__setattr__(self, "group_members", tuple(self.group_members or ()))
        object.__setattr__(self, "allowed_targets", tuple(self.allowed_targets or (self.group,)))


class AgentStore:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    dedup_key TEXT UNIQUE,
                    chat_id TEXT NOT NULL,
                    sender TEXT,
                    content TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_chat_time ON events(chat_id, observed_at);
                CREATE TABLE IF NOT EXISTS event_uploads (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 10,
                    next_attempt_at REAL,
                    lease_until REAL,
                    lease_token TEXT,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_uploads_ready ON event_uploads(status, next_attempt_at, updated_at);
                CREATE TABLE IF NOT EXISTS outbox (
                    command_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    retryable INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    lease_until REAL,
                    lease_token TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(status, next_attempt_at, created_at);
                """
            )
            for statement in (
                "ALTER TABLE outbox ADD COLUMN lease_token TEXT",
                "ALTER TABLE event_uploads ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 10",
                "ALTER TABLE event_uploads ADD COLUMN lease_until REAL",
                "ALTER TABLE event_uploads ADD COLUMN lease_token TEXT",
            ):
                try:
                    self._connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

    def set_state(self, state: str, error: str | None = None) -> None:
        self.set_meta("state", state)
        self.set_meta("state_error", error or "")

    def get_state(self) -> str:
        return self.get_meta("state", "stopped")

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO agent_meta(key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM agent_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def record_event(self, event: MessageEvent) -> bool:
        key = event.dedup_key()
        dedup_key = None
        if key is not None:
            dedup_key = hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        now = self._clock()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, dedup_key, chat_id, sender, content,
                    direction, kind, observed_at, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    dedup_key,
                    event.chat_id,
                    event.sender,
                    event.content,
                    event.direction.value,
                    event.kind.value,
                    event.observed_at,
                    json.dumps(event.to_dict(), ensure_ascii=False, default=str),
                    now,
                ),
            )
            if cursor.rowcount:
                self._connection.execute(
                    "INSERT OR IGNORE INTO event_uploads(event_id, status, attempts, next_attempt_at, last_error, updated_at) VALUES (?, 'pending', 0, ?, NULL, ?)",
                    (event.event_id, now, now),
                )
            return cursor.rowcount == 1

    def recent_events(self, limit: int = 100, chat_id: str | None = None) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        with self._lock:
            if chat_id is None:
                rows = self._connection.execute(
                    "SELECT payload FROM events ORDER BY observed_at DESC, created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload FROM events WHERE chat_id = ? ORDER BY observed_at DESC, created_at DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def pending_event_uploads(self, limit: int = 100) -> list[tuple[str, dict[str, Any]]]:
        if limit < 1:
            return []
        now = self._clock()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT events.event_id, events.payload
                FROM event_uploads
                JOIN events ON events.event_id = event_uploads.event_id
                WHERE event_uploads.status = 'pending'
                  AND (event_uploads.next_attempt_at IS NULL OR event_uploads.next_attempt_at <= ?)
                ORDER BY event_uploads.updated_at, events.event_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [(str(row["event_id"]), json.loads(row["payload"])) for row in rows]

    def claim_event_uploads(self, limit: int = 100, lease_seconds: float = 60) -> list[tuple[str, dict[str, Any], str]]:
        if limit < 1 or lease_seconds <= 0:
            return []
        now = self._clock()
        claimed: list[tuple[str, dict[str, Any], str]] = []
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE event_uploads
                SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                    next_attempt_at = CASE WHEN attempts >= max_attempts THEN NULL ELSE next_attempt_at END,
                    lease_until = NULL, lease_token = NULL, updated_at = ?
                WHERE status = 'processing' AND (lease_until IS NULL OR lease_until < ?)
                """,
                (now, now),
            )
            self._connection.execute(
                """
                UPDATE event_uploads
                SET status = 'failed', next_attempt_at = NULL, updated_at = ?
                WHERE status = 'pending' AND attempts >= max_attempts
                """,
                (now,),
            )
            rows = self._connection.execute(
                """
                SELECT events.event_id, events.payload
                FROM event_uploads
                JOIN events ON events.event_id = event_uploads.event_id
                WHERE event_uploads.status = 'pending'
                  AND (event_uploads.next_attempt_at IS NULL OR event_uploads.next_attempt_at <= ?)
                ORDER BY event_uploads.updated_at, events.event_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                token = uuid4().hex
                cursor = self._connection.execute(
                    """
                    UPDATE event_uploads
                    SET status = 'processing', attempts = attempts + 1,
                        lease_until = ?, lease_token = ?, updated_at = ?
                    WHERE event_id = ? AND status = 'pending'
                    """,
                    (now + lease_seconds, token, now, row["event_id"]),
                )
                if cursor.rowcount:
                    claimed.append((str(row["event_id"]), json.loads(row["payload"]), token))
        return claimed

    def mark_event_uploaded(self, event_id: str, lease_token: str | None = None) -> bool:
        with self._lock, self._connection:
            if lease_token is None:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = 'uploaded', next_attempt_at = NULL, lease_until = NULL, lease_token = NULL, last_error = NULL, updated_at = ? WHERE event_id = ? AND status IN ('pending', 'processing')",
                    (self._clock(), event_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = 'uploaded', next_attempt_at = NULL, lease_until = NULL, lease_token = NULL, last_error = NULL, updated_at = ? WHERE event_id = ? AND status = 'processing' AND lease_token = ?",
                    (self._clock(), event_id, lease_token),
                )
            return cursor.rowcount == 1

    def mark_event_upload_failed(
        self,
        event_id: str,
        error: str,
        retry_delay: float = 5.0,
        lease_token: str | None = None,
    ) -> bool:
        now = self._clock()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT attempts, max_attempts FROM event_uploads WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            status = "failed" if exhausted else "pending"
            next_attempt = None if exhausted else now + max(0.0, retry_delay)
            if lease_token is None:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = ?, next_attempt_at = ?, lease_until = NULL, lease_token = NULL, last_error = ?, updated_at = ? WHERE event_id = ? AND status IN ('pending', 'processing')",
                    (status, next_attempt, str(error)[:1000], now, event_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = ?, next_attempt_at = ?, lease_until = NULL, lease_token = NULL, last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'processing' AND lease_token = ?",
                    (status, next_attempt, str(error)[:1000], now, event_id, lease_token),
                )
            return cursor.rowcount == 1

    def pending_upload_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM event_uploads WHERE status IN ('pending', 'processing')"
            ).fetchone()
        return int(row["count"])

    def failed_upload_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM event_uploads WHERE status = 'failed'"
            ).fetchone()
        return int(row["count"])

    def event_count(self, chat_id: str | None = None) -> int:
        with self._lock:
            if chat_id is None:
                row = self._connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
            else:
                row = self._connection.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
        return int(row["count"])

    def enqueue(self, command: AgentCommand) -> bool:
        now = self._clock()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    command_id, action, target, payload, max_attempts,
                    retryable, status, attempts, next_attempt_at,
                    lease_until, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, NULL, NULL, ?, ?)
                """,
                (
                    command.command_id,
                    command.action,
                    command.target,
                    json.dumps(dict(command.payload), ensure_ascii=False, default=str),
                    command.max_attempts,
                    int(command.retryable),
                    now,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT command_id, action, target, payload, max_attempts,
                       retryable, status, attempts, last_error
                FROM outbox WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "command_id": row["command_id"],
            "action": row["action"],
            "target": row["target"],
            "payload": json.loads(row["payload"]),
            "max_attempts": int(row["max_attempts"]),
            "retryable": bool(row["retryable"]),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"] or "",
        }

    def claim_outbox(self, limit: int = 10, lease_seconds: float = 60) -> list[AgentCommand]:
        return [lease.command for lease in self.claim_outbox_leases(limit, lease_seconds)]

    def claim_outbox_leases(self, limit: int = 10, lease_seconds: float = 60) -> list[OutboxLease]:
        if limit < 1 or lease_seconds <= 0:
            return []
        now = self._clock()
        leases: list[OutboxLease] = []
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE outbox
                SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                    lease_until = NULL, lease_token = NULL, updated_at = ?
                WHERE status = 'processing' AND (lease_until IS NULL OR lease_until < ?)
                """,
                (now, now),
            )
            rows = self._connection.execute(
                """
                SELECT * FROM outbox
                WHERE attempts < max_attempts
                  AND (status = 'queued' OR (status = 'failed' AND retryable = 1))
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at, command_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                token = uuid4().hex
                cursor = self._connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'processing', attempts = attempts + 1,
                        lease_until = ?, lease_token = ?, updated_at = ?
                    WHERE command_id = ? AND status IN ('queued', 'failed')
                    """,
                    (now + lease_seconds, token, now, row["command_id"]),
                )
                if cursor.rowcount:
                    command = AgentCommand(
                        command_id=row["command_id"],
                        action=row["action"],
                        target=row["target"],
                        payload=json.loads(row["payload"]),
                        max_attempts=int(row["max_attempts"]),
                        retryable=bool(row["retryable"]),
                        created_at=float(row["created_at"]),
                    )
                    leases.append(OutboxLease(command=command, token=token, attempt=int(row["attempts"]) + 1))
        return leases

    def release_lease(self, command_id: str, lease_token: str) -> bool:
        now = self._clock()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE outbox SET status = 'queued', lease_until = NULL, lease_token = NULL, updated_at = ? WHERE command_id = ? AND status = 'processing' AND lease_token = ?",
                (now, command_id, lease_token),
            )
            return cursor.rowcount == 1

    def complete(self, command_id: str, lease_token: str | None = None) -> bool:
        now = self._clock()
        with self._lock, self._connection:
            if lease_token is None:
                cursor = self._connection.execute(
                    "UPDATE outbox SET status = 'succeeded', lease_until = NULL, lease_token = NULL, last_error = NULL, updated_at = ? WHERE command_id = ? AND status IN ('queued', 'failed')",
                    (now, command_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE outbox SET status = 'succeeded', lease_until = NULL, lease_token = NULL, last_error = NULL, updated_at = ? WHERE command_id = ? AND status = 'processing' AND lease_token = ?",
                    (now, command_id, lease_token),
                )
            return cursor.rowcount == 1

    def fail(self, command_id: str, error: str, retry_delay: float = 5.0, lease_token: str | None = None) -> bool:
        now = self._clock()
        message = str(error)[:1000]
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT attempts, max_attempts, retryable FROM outbox WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                return False
            retry = bool(row["retryable"]) and int(row["attempts"]) < int(row["max_attempts"])
            status = "queued" if retry else "failed"
            next_attempt = now + max(0.0, retry_delay) if retry else None
            if lease_token is None:
                cursor = self._connection.execute(
                    "UPDATE outbox SET status = ?, next_attempt_at = ?, lease_until = NULL, lease_token = NULL, last_error = ?, updated_at = ? WHERE command_id = ? AND status IN ('queued', 'failed')",
                    (status, next_attempt, message, now, command_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE outbox SET status = ?, next_attempt_at = ?, lease_until = NULL, lease_token = NULL, last_error = ?, updated_at = ? WHERE command_id = ? AND status = 'processing' AND lease_token = ?",
                    (status, next_attempt, message, now, command_id, lease_token),
                )
            return cursor.rowcount == 1

    def recover_outbox(self) -> int:
        now = self._clock()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE outbox
                SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                    lease_until = NULL, lease_token = NULL, updated_at = ?
                WHERE status = 'processing' AND (lease_until IS NULL OR lease_until < ?)
                """,
                (now, now),
            )
            return cursor.rowcount

    def pending_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM outbox WHERE (status IN ('queued', 'processing') OR (status = 'failed' AND retryable = 1)) AND attempts < max_attempts"
            ).fetchone()
        return int(row["count"])

    def health(self) -> dict[str, Any]:
        return {
            "state": self.get_state(),
            "state_error": self.get_meta("state_error", ""),
            "event_count": self.event_count(),
            "pending_uploads": self.pending_upload_count(),
            "failed_uploads": self.failed_upload_count(),
            "pending_commands": self.pending_count(),
            "database": self.path,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class LocalAgent:
    def __init__(
        self,
        config: AgentConfig,
        store: AgentStore | None = None,
        history_reader: Callable[..., list[dict[str, Any]]] | None = None,
        dialog_opener: Callable[..., Any] | None = None,
        listener: Callable[..., dict[str, Any]] | None = None,
        sender: Callable[..., Any] | None = None,
        cloud: Any | None = None,
    ):
        self.config = config
        self.store = store or AgentStore(config.db_path)
        self.history_reader = history_reader
        self.dialog_opener = dialog_opener
        self.listener = listener
        self.sender = sender
        self.cloud = cloud
        self._cloud_error = ""
        self.window_generation = config.window_generation or self.store.get_meta("window_generation") or uuid4().hex
        self._message_sequence = int(self.store.get_meta("message_sequence", "0") or 0)
        if config.window_generation is None:
            self.store.set_meta("window_generation", self.window_generation)
        self._ui_queue = UiOperationQueue(thread_name=f"{config.agent_id}-ui")
        self._cloud_command_ids: set[str] = set()
        self._owns_store = store is None

    def _read_history(self) -> list[dict[str, Any]]:
        if not self.config.bootstrap_history or self.config.history_limit < 1:
            return []
        reader = self.history_reader
        if reader is None:
            from .WeChatAuto import Messages

            reader = lambda **kwargs: Messages.dump_chat_history(**kwargs)
        with _UI_OPERATION_LOCK:
            return list(
                reader(
                    friend=self.config.group,
                    number=self.config.history_limit,
                    groupMembers=list(self.config.group_members),
                    is_json=False,
                    save_detail=False,
                    close_weixin=False,
                    search_pages=5,
                    is_maximize=False,
                )
                or []
            )

    def _open_dialog(self) -> Any:
        opener = self.dialog_opener
        if opener is None:
            from .WeChatTools import Navigator

            opener = Navigator.open_seperate_dialog_window
        from .Errors import NoSuchFriendError

        last_error = None
        for attempt in range(3):
            try:
                with _UI_OPERATION_LOCK:
                    return opener(
                        friend=self.config.group,
                        select=False,
                        is_maximize=False,
                        window_minimize=self.config.background,
                        close_weixin=False,
                    )
            except NoSuchFriendError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5)
        raise last_error

    def _listen(
        self,
        dialog: Any,
        stop_event: threading.Event | None = None,
        sequence_start: int = 0,
    ) -> dict[str, Any]:
        listener = self.listener
        if listener is None:
            from .WeChatAuto import Monitor

            listener = Monitor.listen_on_chat
        return listener(
            dialog_window=dialog,
            duration=self.config.listen_duration,
            groupMembers=list(self.config.group_members),
            close_dialog_window=False,
            account_id=self.config.account_id,
            return_events=True,
            window_generation=self.window_generation,
            stop_event=stop_event,
            sequence_start=sequence_start,
        )

    def _send(self, command: AgentCommand) -> Any:
        if command.target not in self.config.allowed_targets:
            raise PermissionError("command target is not allowed by the agent")
        sender = self.sender
        if sender is None:
            from .WeChatAuto import Messages

            sender = lambda friend, messages: Messages.send_messages_to_friend(
                friend=friend,
                messages=messages,
                close_weixin=False,
            )
        if command.action == "send_text":
            text = str(command.payload.get("text", ""))
            if not text.strip():
                raise ValueError("send_text requires text")
            with _UI_OPERATION_LOCK:
                return sender(command.target, [text])
        if command.action == "send_messages":
            messages = command.payload.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("send_messages requires a non-empty messages list")
            if any(not isinstance(message, str) or not message.strip() for message in messages):
                raise ValueError("send_messages requires non-empty text messages")
            with _UI_OPERATION_LOCK:
                return sender(command.target, list(messages))
        if command.action == "noop":
            return None
        raise ValueError(f"unsupported command action: {command.action}")

    def _record_event(self, event: MessageEvent) -> bool:
        inserted = self.store.record_event(event)
        if self.cloud is None:
            self.store.mark_event_uploaded(event.event_id)
        return inserted

    def _history_event(self, item: Mapping[str, Any], ordinal: int = 0) -> MessageEvent:
        content = str(item.get("消息内容", ""))
        sender = item.get("消息发送人")
        raw = json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str)
        stable_id = "history:" + hashlib.sha256(raw.encode("utf-8")).hexdigest() + f":{ordinal}"
        return MessageEvent(
            event_id=stable_id,
            stable_id=stable_id,
            chat_id=self.config.group,
            account_id=self.config.account_id,
            sender=None if sender is None else str(sender),
            content=content,
            metadata={"source": "history", "raw": dict(item)},
        )

    def _publish(self, events: list[MessageEvent]) -> None:
        del events
        if self.cloud is None:
            return
        pending = self.store.claim_event_uploads(lease_seconds=60)
        if not pending:
            return
        try:
            self.cloud.send_events([payload for _, payload, _ in pending])
        except Exception as exc:
            self._cloud_error = str(exc)
            for event_id, _, token in pending:
                self.store.mark_event_upload_failed(event_id, str(exc), retry_delay=5, lease_token=token)
            self.store.set_state("degraded", str(exc))
            return
        for event_id, _, token in pending:
            self.store.mark_event_uploaded(event_id, lease_token=token)

    def _live_event(self, item: Mapping[str, Any]) -> MessageEvent:
        event = MessageEvent.from_dict(item)
        if event.stable_id is not None:
            return replace(event, window_generation=event.window_generation or self.window_generation)
        if event.sequence is not None:
            return replace(event, window_generation=event.window_generation or self.window_generation)
        if event.runtime_id is None:
            return replace(event, window_generation=event.window_generation or self.window_generation)
        identity = json.dumps(
            [
                self.config.account_id,
                event.chat_id,
                self.window_generation,
                repr(event.runtime_id),
                event.sender or "",
                event.kind.value,
                event.normalized_content,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        runtime_event_id = "runtime:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return replace(event, event_id=runtime_event_id, stable_id=None, window_generation=self.window_generation)

    def bootstrap_history(self, force: bool = False) -> int:
        if not force and self.store.get_meta("history_bootstrapped") == "1":
            return 0
        events = [self._history_event(item, ordinal) for ordinal, item in enumerate(self._read_history())]
        new_events = [event for event in events if self._record_event(event)]
        self._publish(new_events)
        if self.config.bootstrap_history:
            self.store.set_meta("history_bootstrapped", "1")
        return len(new_events)

    def listen_once(self, stop_event: threading.Event | None = None) -> int:
        if self.config.listen_duration in {"", "0", "0s", "0min", "0h"}:
            return 0
        if stop_event is not None and stop_event.is_set():
            return 0
        dialog = None
        try:
            dialog = self._open_dialog()
            result = self._listen(dialog, stop_event=stop_event, sequence_start=self._message_sequence)
        finally:
            if dialog is not None:
                try:
                    dialog.close()
                except Exception:
                    pass
        raw_events = result.get("消息事件", [])
        events = [self._live_event(item) for item in raw_events]
        self._message_sequence += len(raw_events)
        self.store.set_meta("message_sequence", str(self._message_sequence))
        new_events = [event for event in events if self._record_event(event)]
        self._publish(new_events)
        return len(new_events)

    def _acknowledge_cloud(self, command_id: str, result: Mapping[str, Any]) -> bool:
        if self.cloud is None:
            return False
        try:
            self.cloud.acknowledge(command_id, dict(result))
            return True
        except Exception as exc:
            self._cloud_error = str(exc)
            self.store.set_state("degraded", str(exc))
            return False

    def _reconcile_cloud_command(self, command: AgentCommand, queued: bool) -> None:
        if self.cloud is None:
            return
        self._cloud_command_ids.add(command.command_id)
        if queued:
            return
        existing = self.store.get_command(command.command_id)
        if existing is None:
            return
        if existing["status"] == "succeeded":
            if self._acknowledge_cloud(command.command_id, {"state": "completed", "result": "already completed"}):
                self._cloud_command_ids.discard(command.command_id)
        elif existing["status"] == "failed" and (
            not existing["retryable"] or existing["attempts"] >= existing["max_attempts"]
        ):
            if self._acknowledge_cloud(
                command.command_id,
                {"state": "failed", "error": existing["last_error"]},
            ):
                self._cloud_command_ids.discard(command.command_id)
        else:
            self._acknowledge_cloud(command.command_id, {"state": "queued"})

    def sync_cloud_commands(self, stop_event: threading.Event | None = None) -> int:
        if self.cloud is None or (stop_event is not None and stop_event.is_set()):
            return 0
        try:
            items = self.cloud.fetch_commands()
        except Exception as exc:
            self._cloud_error = str(exc)
            self.store.set_state("degraded", str(exc))
            return 0
        synced = 0
        for item in items:
            try:
                command = AgentCommand.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                command_id = str(item.get("command_id", "")).strip()
                if command_id:
                    self._acknowledge_cloud(command_id, {"state": "rejected", "error": str(exc)})
                continue
            if command.target not in self.config.allowed_targets:
                self._acknowledge_cloud(command.command_id, {"state": "rejected", "error": "target not allowed"})
                continue
            queued = self.enqueue(command)
            if queued:
                synced += 1
                self._cloud_command_ids.add(command.command_id)
                self._acknowledge_cloud(command.command_id, {"state": "queued"})
            else:
                self._reconcile_cloud_command(command, queued)
        return synced

    def enqueue(self, command: AgentCommand) -> bool:
        return self.store.enqueue(command)

    def process_outbox(self, stop_event: threading.Event | None = None) -> int:
        processed = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return processed
            leases = self.store.claim_outbox_leases(limit=1, lease_seconds=300)
            if not leases:
                return processed
            lease = leases[0]
            if stop_event is not None and stop_event.is_set():
                self.store.release_lease(lease.command.command_id, lease.token)
                return processed
            try:
                future = self._ui_queue.submit(self._send, lease.command)
                future.result()
                if not self.store.complete(lease.command.command_id, lease.token):
                    continue
                processed += 1
                if lease.command.command_id in self._cloud_command_ids:
                    if self._acknowledge_cloud(lease.command.command_id, {"state": "completed"}):
                        self._cloud_command_ids.discard(lease.command.command_id)
            except Exception as exc:
                if not self.store.fail(lease.command.command_id, str(exc), retry_delay=5, lease_token=lease.token):
                    continue
                if lease.command.command_id in self._cloud_command_ids:
                    current = self.store.get_command(lease.command.command_id)
                    if current is not None and current["retryable"] and current["attempts"] < current["max_attempts"]:
                        self._acknowledge_cloud(lease.command.command_id, {"state": "queued", "error": str(exc)})
                    else:
                        if self._acknowledge_cloud(lease.command.command_id, {"state": "failed", "error": str(exc)}):
                            self._cloud_command_ids.discard(lease.command.command_id)

    def _summary(
        self,
        history_events: int = 0,
        live_events: int = 0,
        synced_commands: int = 0,
        processed_commands: int = 0,
    ) -> dict[str, Any]:
        return {
            "history_events": history_events,
            "live_events": live_events,
            "synced_commands": synced_commands,
            "processed_commands": processed_commands,
            "health": self.store.health(),
        }

    def run_once(self, stop_event: threading.Event | None = None) -> dict[str, Any]:
        if stop_event is not None and stop_event.is_set():
            return self._summary()
        self._cloud_error = ""
        self.store.set_state("starting")
        try:
            history_count = self.bootstrap_history()
            if stop_event is not None and stop_event.is_set():
                return self._summary(history_events=history_count)
            self.store.set_state("listening")
            live_count = self.listen_once(stop_event=stop_event)
            if stop_event is not None and stop_event.is_set():
                return self._summary(history_events=history_count, live_events=live_count)
            synced_commands = self.sync_cloud_commands(stop_event=stop_event)
            processed_count = self.process_outbox(stop_event=stop_event)
            if self.store.pending_upload_count():
                self.store.set_state("degraded", self.store.get_meta("state_error") or "cloud event upload pending")
            elif self._cloud_error:
                self.store.set_state("degraded", self._cloud_error)
            else:
                self.store.set_state("idle")
            if self.cloud is not None:
                try:
                    self.cloud.heartbeat(self.store.health())
                except Exception as exc:
                    self._cloud_error = str(exc)
                    self.store.set_state("degraded", str(exc))
            return self._summary(history_count, live_count, synced_commands, processed_count)
        except Exception as exc:
            self.store.set_state("degraded", str(exc))
            if self.cloud is not None:
                try:
                    self.cloud.heartbeat(self.store.health())
                except Exception:
                    pass
            raise

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.store.recover_outbox()
            try:
                self.run_once(stop_event=stop_event)
            except Exception:
                pass
            stop_event.wait(self.config.poll_interval)

    def close(self) -> None:
        self._ui_queue.close()
        if self.store.get_state() != "degraded":
            self.store.set_state("stopped")
        if self._owns_store:
            self.store.close()
