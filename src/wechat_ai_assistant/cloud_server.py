from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import threading
import time
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from .agent import AgentCommand
from .console import CONSOLE_HTML
from .orders import OpenAICompatibleParser, OrderStore


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

    def list_agents(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._lock:
            rows = self._connection.execute(
                "SELECT agent_id, last_health, updated_at FROM agents ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "agent_id": row["agent_id"],
                "health": json.loads(row["last_health"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def recent_events(self, agent_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self._lock:
            if agent_id:
                rows = self._connection.execute(
                    "SELECT agent_id, event_id, payload, created_at FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                    (agent_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT agent_id, event_id, payload, created_at FROM events ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "agent_id": row["agent_id"],
                "event_id": row["event_id"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_command_history(self, agent_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self._lock:
            if agent_id:
                rows = self._connection.execute(
                    "SELECT command_id, agent_id, action, target, payload, max_attempts, retryable, status, result, created_at, updated_at FROM commands WHERE agent_id = ? ORDER BY created_at DESC, command_id DESC LIMIT ?",
                    (agent_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT command_id, agent_id, action, target, payload, max_attempts, retryable, status, result, created_at, updated_at FROM commands ORDER BY created_at DESC, command_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "command_id": row["command_id"],
                "agent_id": row["agent_id"],
                "action": row["action"],
                "target": row["target"],
                "payload": json.loads(row["payload"]),
                "max_attempts": int(row["max_attempts"]),
                "retryable": bool(row["retryable"]),
                "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

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
    console_token: str = ""
    agent_tokens: Mapping[str, str] | None = None
    max_body_size: int = 2 * 1024 * 1024
    order_store: OrderStore | None = None

    def log_message(self, format, *args):
        del format, args

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _console_authorized(self) -> bool:
        if not self.console_token:
            return False
        authorization = self.headers.get("Authorization", "")
        try:
            return hmac.compare_digest(authorization, f"Bearer {self.console_token}")
        except TypeError:
            return False

    def _console_state(self, agent_id: str = "") -> dict[str, Any]:
        return {
            "server_time": time.time(),
            "health": self.store.health(),
            "agents": self.store.list_agents(),
            "events": self.store.recent_events(agent_id),
            "commands": self.store.list_command_history(agent_id),
        }

    def _merchant(self) -> dict[str, str] | None:
        if self.order_store is None:
            return None
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        return self.order_store.authenticate(authorization[7:])

    def _device_merchant(self) -> dict[str,str] | None:
        if self.order_store is None:return None
        value=self.headers.get("Authorization","")
        if not value.startswith("Bearer "):return None
        identity=self.order_store.authenticate(value[7:])
        return identity if identity and identity.get("role")=="device" else None

    def _merchant_result(self, result: Mapping[str, Any]) -> None:
        self._send_json(200, result)

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        if self.order_store is not None and authorization.startswith("Bearer "):
            bound=self.order_store.authenticate(authorization[7:])
            if bound and bound.get("role")=="device":return True
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
        if parsed.path in {"/", "/console"}:
            self._send_html(200, CONSOLE_HTML)
            return
        if parsed.path == "/api/v1/console/state":
            if not self._console_authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            query = parse_qs(parsed.query)
            agent_id = query.get("agent_id", [""])[0].strip()
            self._send_json(200, self._console_state(agent_id))
            return
        if parsed.path.startswith("/api/v1/business/"):
            identity = self._merchant()
            if not identity or identity["role"] == "device":
                self._send_json(401, {"error": "merchant authentication required"}); return
            tenant = identity["merchant_id"]
            store = self.order_store
            if parsed.path == "/api/v1/business/dashboard":
                self._send_json(200, store.dashboard(tenant)); return
            if parsed.path == "/api/v1/business/orders":
                status = parse_qs(parsed.query).get("status", [""])[0]
                self._send_json(200, {"orders": [store.order_detail(tenant,o["id"]) for o in store.list_orders(tenant,status)]}); return
            if parsed.path == "/api/v1/business/customers":
                rows=store.db.execute("SELECT * FROM customers WHERE merchant_id=? ORDER BY name",(tenant,)).fetchall();self._send_json(200,{"customers":[dict(r) for r in rows]});return
            if parsed.path == "/api/v1/business/products":
                rows=store.db.execute("SELECT * FROM products WHERE merchant_id=? ORDER BY name",(tenant,)).fetchall();self._send_json(200,{"products":[dict(r) for r in rows]});return
            if parsed.path == "/api/v1/business/imports":
                rows=store.db.execute("SELECT * FROM imports WHERE merchant_id=? ORDER BY created_at DESC",(tenant,)).fetchall();self._send_json(200,{"imports":[dict(r) for r in rows]});return
            if parsed.path == "/api/v1/business/export":
                orders=store.list_orders(tenant,"confirmed");lines=["order_id,customer,status,SKU,product,quantity,unit,unit_price,amount"]
                for order in orders:
                    for item in store.order_detail(tenant,order["id"])["items"]: lines.append(",".join(str(v) for v in [order["id"],order["customer_name"],order["status"],item["sku"],item["product_name"],item["quantity"],item["unit"],item["unit_price_cents"]/100,item["quantity"]*item["unit_price_cents"]/100]))
                body=("\ufeff"+"\n".join(lines)).encode("utf-8");self.send_response(200);self.send_header("Content-Type","text/csv; charset=utf-8");self.send_header("Content-Disposition","attachment; filename=orders.csv");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);return
            self._send_json(404,{"error":"not found"});return
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

    def _enqueue_console_command(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip()
        action = str(payload.get("action", "")).strip()
        target = str(payload.get("target", "")).strip()
        command_payload = payload.get("payload", {})
        if not agent_id or not action or not target:
            raise ValueError("agent_id, action and target are required")
        if not isinstance(command_payload, Mapping):
            raise ValueError("payload must be an object")
        if action not in {"send_text", "send_messages", "noop"}:
            raise ValueError("unsupported console action")
        if action == "send_text" and not str(command_payload.get("text", "")).strip():
            raise ValueError("send_text requires text")
        if action == "send_messages" and (
            not isinstance(command_payload.get("messages"), list) or not command_payload["messages"]
        ):
            raise ValueError("send_messages requires a non-empty list")
        try:
            max_attempts = int(payload.get("max_attempts", 3))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_attempts must be an integer") from exc
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        command_id = str(payload.get("command_id", "")).strip() or f"console-{uuid4().hex}"
        command = AgentCommand(
            command_id=command_id,
            action=action,
            target=target,
            payload=command_payload,
            max_attempts=max_attempts,
            retryable=bool(payload.get("retryable", False)),
        )
        created = self.store.enqueue(agent_id, command)
        return {"command_id": command.command_id, "agent_id": agent_id, "created": created}

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/agent/business-events":
            identity=self._device_merchant()
            if not identity:self._send_json(401,{"error":"bound device token required"});return
            try:
                agent_id=self._agent_id();payload=self._read_json();events=payload.get("events",[])
                if not agent_id or not isinstance(events,list):raise ValueError("X-Agent-Id and events list are required")
                result=self.order_store.ingest_agent_events(identity["merchant_id"],identity["id"],agent_id,events);self._send_json(200,result)
            except ValueError as exc:self._send_json(400,{"error":str(exc)})
            return
        if parsed.path.startswith("/api/v1/business/"):
            if parsed.path == "/api/v1/business/register":
                try:
                    if self.order_store is None: raise ValueError("business database is not enabled; set WECHAT_AI_BUSINESS_DB")
                    payload=self._read_json()
                    result=self.order_store.create_merchant(str(payload.get("name","示例商家")),plan="trial",max_users=5,max_groups=3,ai_quota=0)
                    self._send_json(201,result)
                except (ValueError,KeyError) as exc:self._send_json(400,{"error":str(exc)})
                except Exception as exc:self._send_json(500,{"error":str(exc)})
                return
            if parsed.path == "/api/v1/business/devices":
                identity=self._merchant()
                if not identity or identity["role"]=="device":self._send_json(401,{"error":"merchant authentication required"});return
                try:
                    payload=self._read_json();device=self.order_store.create_device(identity["merchant_id"],str(payload.get("name","本地采集器")))
                    for group in payload.get("groups",[]):self.order_store.bind_device_group(identity["merchant_id"],device["device_id"],str(group),str(payload.get("account_id","default")))
                    self._send_json(201,device)
                except Exception as exc:self._send_json(400,{"error":str(exc)})
                return
            identity=self._merchant()
            if not identity or identity["role"] == "device": self._send_json(401,{"error":"merchant authentication required"});return
            tenant=identity["merchant_id"]
            store=self.order_store
            try:
                payload=self._read_json()
                if parsed.path == "/api/v1/business/customers":
                    cid=store.add_customer(tenant,str(payload["name"]),str(payload["alias"]),str(payload.get("group","demo-group")),str(payload.get("address","")));self._send_json(200,{"customer_id":cid});return
                if parsed.path == "/api/v1/business/products":
                    pid=store.add_product(tenant,str(payload["sku"]),str(payload["name"]),str(payload["unit"]),int(payload["price_cents"]),payload.get("aliases",[]));self._send_json(200,{"product_id":pid});return
                if parsed.path == "/api/v1/business/imports":
                    messages=payload.get("messages",[])
                    if not isinstance(messages,list):raise ValueError("messages must be a list")
                    result=store.add_import(tenant,str(payload.get("group","demo-group")),messages,str(payload.get("source") or uuid4().hex),str(payload.get("source_type","demo")),str(payload.get("known_gap","")));self._send_json(200,result);return
                if parsed.path == "/api/v1/business/parse":
                    result=store.parse_import(tenant,str(payload.get("group","demo-group")),payload.get("import_id"));self._send_json(200,result);return
                if parsed.path == "/api/v1/business/bind-customer":
                    message_id=str(payload["message_id"]);store.bind_message_customer(tenant,message_id,str(payload["customer_id"]),identity["id"])
                    msg=store.db.execute("SELECT group_id,import_id FROM messages WHERE merchant_id=? AND id=?",(tenant,message_id)).fetchone()
                    parsed_result=store.parse_import(tenant,msg["group_id"],msg["import_id"])
                    self._send_json(200,{"ok":True,"parse":parsed_result});return
                if parsed.path == "/api/v1/business/resolve-message-match":
                    message_id=str(payload["message_id"]);duplicate=payload.get("is_duplicate")
                    if not isinstance(duplicate,bool):raise ValueError("is_duplicate must be a boolean")
                    self._send_json(200,store.resolve_message_match(tenant,message_id,duplicate,identity["id"]));return
                if parsed.path.endswith("/correct"):
                    oid=unquote(parsed.path.split("/")[-2]);new_id=store.correct_order(tenant,oid,identity["id"],payload.get("customer_id"),payload.get("items"));self._send_json(200,{"order_id":new_id});return
                if parsed.path.endswith("/confirm"):
                    oid=unquote(parsed.path.split("/")[-2]);store.confirm(tenant,oid,identity["id"]);self._send_json(200,{"ok":True});return
                self._send_json(404,{"error":"not found"})
            except (ValueError,KeyError,PermissionError,sqlite3.IntegrityError) as exc:self._send_json(400,{"error":str(exc)})
            except Exception:self._send_json(500,{"error":"internal server error"})
            return
        if parsed.path == "/api/v1/console/commands":
            if not self._console_authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                self._send_json(200, self._enqueue_console_command(self._read_json()))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception:
                self._send_json(500, {"error": "internal server error"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        agent_id = self._agent_id()
        if not agent_id:
            self._send_json(400, {"error": "X-Agent-Id is required"})
            return
        if parsed.path == "/api/v1/agent/business-events":
            identity=self._merchant()
            if not identity or identity.get("role")!="device":self._send_json(401,{"error":"bound device token required"});return
            payload=self._read_json();events=payload.get("events",[])
            if not isinstance(events,list):self._send_json(400,{"error":"events must be a list"});return
            result=self.order_store.ingest_agent_events(identity["merchant_id"],identity["id"],agent_id,events);self._send_json(200,result);return
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
    console_token: str | None = None,
    business_db_path: str | None = None,
    model_parser: OpenAICompatibleParser | None = None,
) -> tuple[ThreadingHTTPServer, CloudStore]:
    if not token and not agent_tokens:
        raise ValueError("token or agent_tokens is required")
    if host not in {"127.0.0.1", "localhost", "::1"} and not agent_tokens:
        raise ValueError("agent_tokens is required for non-loopback binds")
    if model_parser is None:
        model_url=os.environ.get("WECHAT_AI_MODEL_BASE_URL","");model_key=os.environ.get("WECHAT_AI_MODEL_API_KEY","");model_name=os.environ.get("WECHAT_AI_MODEL_NAME","")
        configured=bool(model_url or model_key or model_name)
        if configured and not all((model_url,model_key,model_name)):
            raise ValueError("configure WECHAT_AI_MODEL_BASE_URL, WECHAT_AI_MODEL_API_KEY and WECHAT_AI_MODEL_NAME together; partial configuration is not allowed")
        if configured:model_parser=OpenAICompatibleParser(model_url,model_key,model_name)
    store = CloudStore(db_path)

    class Handler(CloudRequestHandler):
        pass

    Handler.store = store
    Handler.auth_token = token
    Handler.console_token = console_token if console_token is not None else token
    Handler.agent_tokens = dict(agent_tokens) if agent_tokens else None
    business_path = business_db_path or (":memory:" if db_path == ":memory:" else str(Path(db_path).with_name(Path(db_path).stem + "_business.db")))
    class Server(ThreadingHTTPServer):
        def server_close(self):
            super().server_close()
            if Handler.order_store is not None:
                Handler.order_store.close()
                Handler.order_store = None

    server = Server((host, port), Handler)
    # Existing device/console-only installs do not open an unused second database.
    Handler.order_store = OrderStore(business_path,parser=model_parser) if business_db_path is not None else None
    return server, store


def _load_agent_tokens() -> dict[str, str]:
    raw = os.environ.get("WECHAT_AI_AGENT_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("WECHAT_AI_AGENT_TOKENS must be a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("WECHAT_AI_AGENT_TOKENS must be a JSON object")
    tokens: dict[str, str] = {}
    for agent_id, token in data.items():
        if not isinstance(agent_id, str) or not agent_id.strip() or not isinstance(token, str) or not token:
            raise ValueError("WECHAT_AI_AGENT_TOKENS values must be non-empty strings")
        tokens[agent_id.strip()] = token
    return tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wechat-ai-assistant-cloud")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=os.environ.get("WECHAT_AI_CLOUD_TOKEN", ""))
    parser.add_argument("--console-token", default=os.environ.get("WECHAT_AI_CONSOLE_TOKEN", ""))
    parser.add_argument("--db", default="cloud.db")
    parser.add_argument("--business-db", default=os.environ.get("WECHAT_AI_BUSINESS_DB", ""))
    args = parser.parse_args(argv)
    agent_tokens = _load_agent_tokens()
    server, store = create_server(
        args.host,
        args.port,
        args.token,
        args.db,
        agent_tokens=agent_tokens,
        console_token=args.console_token or args.token,
        business_db_path=args.business_db or None,
    )
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
