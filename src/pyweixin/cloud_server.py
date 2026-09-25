from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from .agent import AgentCommand


class CloudStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    last_health TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(agent_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS commands_agent_status ON commands(agent_id, status, created_at);
                """
            )
            for statement in (
                "ALTER TABLE commands ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3",
                "ALTER TABLE commands ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    self._connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

    def heartbeat(self, agent_id: str, health: Mapping[str, Any]) -> None:
        now = time.time()
        payload = json.dumps(dict(health), ensure_ascii=False, default=str)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agents(agent_id, last_health, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET last_health=excluded.last_health, updated_at=excluded.updated_at
                """,
                (agent_id, payload, now),
            )

    def ingest_events(self, agent_id: str, events: list[Mapping[str, Any]]) -> int:
        now = time.time()
        inserted = 0
        with self._lock, self._connection:
            for event in events:
                event_id = str(event.get("event_id", ""))
                if not event_id:
                    continue
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO events(agent_id, event_id, payload, created_at) VALUES (?, ?, ?, ?)",
                    (agent_id, event_id, json.dumps(dict(event), ensure_ascii=False, default=str), now),
                )
                inserted += cursor.rowcount
        return inserted

    def enqueue(self, agent_id: str, command: AgentCommand) -> bool:
        now = time.time()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO commands(
                    command_id, agent_id, action, target, payload,
                    max_attempts, retryable, status, result, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?)
                """,
                (
                    command.command_id,
                    agent_id,
                    command.action,
                    command.target,
                    json.dumps(dict(command.payload), ensure_ascii=False, default=str),
                    command.max_attempts,
                    int(command.retryable),
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def list_commands(self, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT command_id, action, target, payload, max_attempts, retryable, created_at
                FROM commands WHERE agent_id = ? AND status = 'queued'
                ORDER BY created_at, command_id LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [
            {
                "command_id": row["command_id"],
                "action": row["action"],
                "target": row["target"],
                "payload": json.loads(row["payload"]),
                "max_attempts": int(row["max_attempts"]),
                "retryable": bool(row["retryable"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def acknowledge(self, agent_id: str, command_id: str, result: Mapping[str, Any]) -> bool:
        state = str(result.get("state", "acknowledged"))
        allowed_states = {"queued", "duplicate", "completed", "succeeded", "failed", "rejected"}
        if state not in allowed_states:
            raise ValueError("unsupported command state")
        next_status = "queued" if state in {"queued", "duplicate"} else "completed" if state == "succeeded" else state
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status FROM commands WHERE command_id = ? AND agent_id = ?",
                (command_id, agent_id),
            ).fetchone()
            if row is None:
                return False
            terminal_states = {"completed", "failed", "rejected"}
            if row["status"] in terminal_states and next_status != row["status"]:
                return False
            cursor = self._connection.execute(
                "UPDATE commands SET status = ?, result = ?, updated_at = ? WHERE command_id = ? AND agent_id = ? AND status = ?",
                (next_status, json.dumps(dict(result), ensure_ascii=False, default=str), time.time(), command_id, agent_id, row["status"]),
            )
            return cursor.rowcount == 1

    def health(self) -> dict[str, int]:
        with self._lock:
            events = self._connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            commands = self._connection.execute("SELECT COUNT(*) AS count FROM commands WHERE status = 'queued'").fetchone()["count"]
            agents = self._connection.execute("SELECT COUNT(*) AS count FROM agents").fetchone()["count"]
        return {"agents": int(agents), "events": int(events), "queued_commands": int(commands)}

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class CloudRequestHandler(BaseHTTPRequestHandler):
    store: CloudStore
    auth_token: str
    agent_tokens: Mapping[str, str] | None = None
    max_body_size: int = 2 * 1024 * 1024

    def log_message(self, format, *args):
        del format, args

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        if self.agent_tokens:
            agent_id = self._agent_id()
            expected_token = self.agent_tokens.get(agent_id, "")
            if not expected_token:
                return False
            try:
                return hmac.compare_digest(authorization, f"Bearer {expected_token}")
            except TypeError:
                return False
        expected = f"Bearer {self.auth_token}"
        try:
            return hmac.compare_digest(authorization, expected)
        except TypeError:
            return False

    def _agent_id(self) -> str:
        return self.headers.get("X-Agent-Id", "").strip()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > self.max_body_size:
            raise ValueError("request body is too large")
        if length == 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"ok": True, **self.store.health()})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        agent_id = self._agent_id()
        if not agent_id:
            self._send_json(400, {"error": "X-Agent-Id is required"})
            return
        if parsed.path == "/api/v1/agent/commands":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                self._send_json(400, {"error": "limit must be an integer"})
                return
            self._send_json(200, {"commands": self.store.list_commands(agent_id, limit)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        agent_id = self._agent_id()
        if not agent_id:
            self._send_json(400, {"error": "X-Agent-Id is required"})
            return
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/v1/agent/heartbeat":
                health = payload.get("health", {})
                if not isinstance(health, Mapping):
                    raise ValueError("health must be an object")
                self.store.heartbeat(agent_id, health)
                self._send_json(200, {"ok": True})
                return
            if parsed.path == "/api/v1/agent/events":
                events = payload.get("events", [])
                if not isinstance(events, list):
                    raise ValueError("events must be a list")
                self._send_json(200, {"inserted": self.store.ingest_events(agent_id, events)})
                return
            if parsed.path.startswith("/api/v1/agent/commands/") and parsed.path.endswith("/ack"):
                command_id = unquote(parsed.path.split("/")[-2])
                result = payload.get("result", {})
                if not isinstance(result, Mapping):
                    raise ValueError("result must be an object")
                self._send_json(200, {"updated": self.store.acknowledge(agent_id, command_id, result)})
                return
            self._send_json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            self._send_json(500, {"error": "internal server error"})


def create_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str = "",
    db_path: str = "cloud.db",
    agent_tokens: Mapping[str, str] | None = None,
) -> tuple[ThreadingHTTPServer, CloudStore]:
    if not token and not agent_tokens:
        raise ValueError("token or agent_tokens is required")
    if host not in {"127.0.0.1", "localhost", "::1"} and not agent_tokens:
        raise ValueError("agent_tokens is required for non-loopback binds")
    store = CloudStore(db_path)

    class Handler(CloudRequestHandler):
        pass

    Handler.store = store
    Handler.auth_token = token
    Handler.agent_tokens = dict(agent_tokens) if agent_tokens else None
    return ThreadingHTTPServer((host, port), Handler), store


def _load_agent_tokens() -> dict[str, str]:
    raw = os.environ.get("PYWEIXIN_AGENT_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("PYWEIXIN_AGENT_TOKENS must be a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("PYWEIXIN_AGENT_TOKENS must be a JSON object")
    tokens: dict[str, str] = {}
    for agent_id, token in data.items():
        if not isinstance(agent_id, str) or not agent_id.strip() or not isinstance(token, str) or not token:
            raise ValueError("PYWEIXIN_AGENT_TOKENS values must be non-empty strings")
        tokens[agent_id.strip()] = token
    return tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyweixin-cloud")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=os.environ.get("PYWEIXIN_CLOUD_TOKEN", ""))
    parser.add_argument("--db", default="cloud.db")
    args = parser.parse_args(argv)
    agent_tokens = _load_agent_tokens()
    server, store = create_server(args.host, args.port, args.token, args.db, agent_tokens=agent_tokens)
    print(f"cloud server listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
