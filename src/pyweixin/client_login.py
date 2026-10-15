"""Windows client for merchant login and cloud order parsing.

The shop owner never sees a token, a key, a provider or a session id. They
click 登录, confirm in a browser, and the client finishes the login by itself.

Design constraints:

* Credentials are protected with DPAPI (``win32crypt``) bound to the Windows
  user account. Nothing is written to a plain config file. On a non-Windows
  host, or when DPAPI is unavailable, the store refuses to persist rather than
  silently writing plaintext.
* Long-term customer memory stays on the merchant's PC. Only the fields needed
  for the current parse are uploaded, and the client is the only component that
  decides what "needed" means.
* Offline behaviour is first class: local order history, manual editing and
  export never depend on the gateway, and unsent messages are retained.
"""
from __future__ import annotations

import json
import os
import platform
import sqlite3
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

# --------------------------------------------------------------------- status

#: Merchant-facing wording. These strings are the only user-visible status text.
STATUS_LOGGED_OUT = "未登录"
STATUS_WAITING_BROWSER = "请在浏览器中确认这台电脑"
STATUS_LOGGED_IN = "登录成功"
STATUS_NETWORK_DOWN = "网络暂不可用"
STATUS_SUBSCRIPTION_EXPIRED = "套餐已到期"
STATUS_QUEUED = "解析排队中"
STATUS_RUNNING = "解析处理中"
STATUS_RETRY = "需要重试"
STATUS_SUCCEEDED = "解析完成"
STATUS_FAILED = "解析失败，请查看原因"
STATUS_CANCELLED = "已取消"
STATUS_UNKNOWN = "结果待确认"

_STATE_LABELS = {
    "queued": STATUS_QUEUED,
    "running": STATUS_RUNNING,
    "succeeded": STATUS_SUCCEEDED,
    "failed": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED,
    "outcome_unknown": STATUS_RETRY,
    "awaiting_reconcile": STATUS_RETRY,
}

_ERROR_LABELS = {
    "network_unavailable": STATUS_NETWORK_DOWN,
    "subscription_inactive": STATUS_SUBSCRIPTION_EXPIRED,
    "quota_exhausted": "剩余额度不足",
    "free_quota_exhausted": "今天的可用分析次数已用完，已为您保留",
    "no_free_model": "暂时没有可用于分析消息的模型，已为您保留",
    "model_unavailable": "所选模型暂不可用，已为您保留",
    "model_not_allowed": "所选模型不可用，请重新选择",
    "platform_budget_exhausted": "服务暂不可用，已为您保留",
    "pricing_changed": "分析价格有变化，请重新确认",
    "pricing_version_required": "提交前需要先确认这次分析的费用",
    "concurrency_limit": "当前解析任务较多，请稍后再试",
    "model_concurrency_limit": "当前解析任务较多，请稍后再试",
    "unauthenticated": "登录已过期，请重新登录",
    "forbidden": "当前设备已被停用，请联系客服",
    "device_revoked": "当前设备已被停用，请联系客服",
    "outcome_unknown": STATUS_UNKNOWN,
    "invalid_output": "解析结果异常，请重试",
    "backend_unavailable": STATUS_NETWORK_DOWN,
    "payload_too_large": "提交内容过大",
    "unsupported_field": "提交内容不被支持",
}

RETRYABLE_ERRORS = {"network_unavailable", "backend_unavailable", "outcome_unknown", "internal_error"}

#: 这些情况**不是**这条消息的错：额度用完、模型暂时不可用、平台暂停接收。
#: 处理方式一律是留在本地队列里等，而不是把消息丢掉。丢掉的话商户会以为
#: "没有待处理的解析"，等他明天再来，订单就已经不见了。
#:
#: ``pricing_changed`` 也在里面：价格在商户确认之后变了，要重新确认，不是
#: 按新价继续扣。
HELD_ERRORS = {
    "quota_exhausted",
    # 下面两个是免费档位的错误码。免费路径已经在服务端焊死，它们不会再被抛出；
    # 这里**故意留着** —— 万一客户端连的是旧版本服务端，收到它们时仍应
    # 保住已读到的数据，而不是把订单丢掉。从"用不到"变成"用不上"只需删两行，
    # 但删了就少了一层兜底。
    "free_quota_exhausted",
    "subscription_inactive",
    "no_free_model",
    "model_unavailable",
    "platform_budget_exhausted",
    "concurrency_limit",
    "model_concurrency_limit",
    "pricing_changed",
    "pricing_version_required",
}

#: 需要**商户重新确认价格**的错误。客户端只把新事实展示出来，
#: 绝**不自动**把版本号更新成新的然后重发 —— 那等于替商户同意了涨价。
PRICING_CONFIRM_ERRORS = {"pricing_changed", "pricing_version_required"}


def status_label(state: str, error_code: str = "") -> str:
    if error_code and error_code in _ERROR_LABELS:
        return _ERROR_LABELS[error_code]
    return _STATE_LABELS.get(state, STATUS_RETRY)


def should_retry(error_code: str, attempts: int, max_attempts: int = 5) -> bool:
    """Whether a pending parse should be retried once the network is back.

    Only transient conditions are retried. A quota or subscription problem is
    not retryable: the merchant has to renew or wait for the period to roll
    over, and hammering the gateway would not help.
    """
    if attempts >= max_attempts:
        return False
    return str(error_code) in RETRYABLE_ERRORS


class LoginError(RuntimeError):
    """Login failed in a way the merchant interface can show directly."""


class NetworkUnavailable(LoginError):
    pass


# ------------------------------------------------------------- credential store


class CredentialStore:
    """DPAPI-protected JSON blob. Never writes a plaintext fallback."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _crypt(self, data: bytes, protect: bool) -> bytes:
        if platform.system() != "Windows":
            raise LoginError("凭据保护仅支持 Windows（DPAPI）。当前系统无法安全保存登录信息。")
        try:
            import win32crypt  # type: ignore
        except ImportError as exc:  # pragma: no cover - pywin32 is a hard dep on Windows
            raise LoginError("缺少 pywin32，无法安全保存登录信息。请安装后重试。") from exc
        if protect:
            # CRYPTPROTECT_UI_FORBIDDEN: no interactive prompt, machine+user scope.
            return win32crypt.CryptProtectData(data, "pyweixin", None, None, None, 0x1)
        return win32crypt.CryptUnprotectData(data, None, None, None, 0x1)[1]

    def save(self, payload: Mapping[str, Any]) -> None:
        blob = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        protected = self._crypt(blob, protect=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_bytes(protected)
            os.replace(temporary, self.path)
            # The blob is only decryptable by this Windows user, but tighten the
            # file mode anyway so a co-tenant account cannot read the ciphertext.
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover - best effort on Windows
                pass

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self._lock:
            try:
                protected = self.path.read_bytes()
            except OSError:
                return None
        try:
            payload = json.loads(self._crypt(protected, protect=False).decode("utf-8"))
        except (LoginError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    self.path.unlink()
                except OSError:  # pragma: no cover
                    pass


# ------------------------------------------------------------------- transport


class GatewayError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0, billing: Mapping[str, Any] | None = None):
        super().__init__(message or code)
        self.code = code or "internal_error"
        self.merchant_message = message or "服务暂时不可用"
        self.status = status
        #: 服务端在某些错误里附带的**机器可读计费事实**（例如价格变了要重新
        #: 确认、平台预算用尽）。客户端只把它转述给商户，不自己重算。
        self.billing = dict(billing or {})


@dataclass
class Session:
    access_token: str
    refresh_token: str
    device_id: str = ""
    merchant_name: str = ""
    device_name: str = ""
    subscription: dict[str, Any] = field(default_factory=dict)
    expires_at: float = 0.0
    provider: str = "product"
    gateway_url: str = ""
    auth_origin: str = ""

    @property
    def remaining_micro_usd(self) -> int:
        return int(self.subscription.get("remaining_micro_usd", 0))

    @property
    def plan_code(self) -> str:
        return str(self.subscription.get("plan_code", ""))


class MerchantClient:
    """HTTP client for the product gateway."""

    def __init__(self, base_url: str, session: Session | None = None, timeout: float = 15.0, allow_insecure_remote: bool = False):
        if not base_url:
            raise ValueError("base_url is required")
        if not base_url.startswith("https://"):
            host = base_url.split("://", 1)[-1].split("/")[0].split(":")[0]
            if host not in {"127.0.0.1", "localhost", "::1"} and not allow_insecure_remote:
                raise ValueError("远程网关必须使用 HTTPS")
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self._opener = build_opener()

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": "pyweixin-shop/1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if authenticated and self.session is not None and self.session.access_token:
            headers["Authorization"] = "Bearer " + self.session.access_token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raw = b""
            try:
                raw = exc.read(256 * 1024)
            except Exception:  # pragma: no cover
                pass
            finally:
                exc.close()
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            error = body.get("error")
            error_fields = error if isinstance(error, Mapping) else body
            raise GatewayError(
                str(error_fields.get("code") or (error if isinstance(error, str) else f"http_{exc.code}")),
                str(error_fields.get("message", "")),
                exc.code,
                billing=body.get("billing"),
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GatewayError("network_unavailable", STATUS_NETWORK_DOWN) from exc
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError as exc:
            raise GatewayError("invalid_response", "服务返回异常，请稍后重试") from exc
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------- device flow

    def start_login(self, device_name: str, platform: str = "windows") -> dict[str, Any]:
        return self._request(
            "POST", "/v1/device/authorize", {"device_name": device_name, "platform": platform}, authenticated=False
        )

    def poll_login(self, device_code: str) -> dict[str, Any]:
        return self._request("POST", "/v1/device/authorize/poll", {"device_code": device_code}, authenticated=False)

    def refresh(self) -> dict[str, Any]:
        if self.session is None or not self.session.refresh_token:
            raise GatewayError("unauthenticated", "登录已过期，请重新登录")
        body = self._request("POST", "/v1/auth/refresh", {"refresh_token": self.session.refresh_token}, authenticated=False)
        self.session.access_token = body["access_token"]
        self.session.refresh_token = body["refresh_token"]
        self.session.expires_at = time.time() + float(body.get("expires_in", 900))
        return body

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/v1/account")

    def logout(self) -> None:
        if self.session is not None and self.session.refresh_token:
            try:
                self._request("POST", "/v1/auth/logout", {"refresh_token": self.session.refresh_token}, authenticated=False)
            except GatewayError:
                # Logging out locally must succeed even when the gateway is down.
                pass

    # ------------------------------------------------------------------- jobs

    def submit_parse(self, request: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/v1/order-parse-jobs", request, idempotency_key=idempotency_key)

    def submit_summary(self, request: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        """提交摘要任务。网关按任务冻结模型与计费，和订单任务同一口径。"""
        return self._request("POST", "/v1/summarize-jobs", request, idempotency_key=idempotency_key)

    def list_models(self) -> dict[str, Any]:
        """客户可见的模型目录。只有别名，不含任何上游信息。"""
        return self._request("GET", "/v1/models")

    def refresh_models(self) -> dict[str, Any]:
        return self._request("GET", "/v1/models/refresh")

    def job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/order-parse-jobs/{job_id}")

    def summary_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/summarize-jobs/{job_id}")

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/order-parse-jobs/{job_id}/cancel", {})

    def cancel_summary(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/summarize-jobs/{job_id}/cancel", {})

    def devices(self) -> dict[str, Any]:
        return self._request("GET", "/v1/devices")


# --------------------------------------------------------------- offline queue


class PendingQueue:
    """Local outbox of parse requests that have not reached the gateway yet.

    Keeping this on the merchant's PC is what lets the shop keep working
    through an outage: messages stay queued with their original content and are
    retried later, while local orders remain fully readable and exportable.
    """

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = str(path)
        self._clock = clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_parses(
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL,
                job_id TEXT,
                last_error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pending_ready ON pending_parses(state, next_attempt_at);
            """
        )
        self._db.commit()

    def enqueue(self, request: Mapping[str, Any], idempotency_key: str = "") -> str:
        key = idempotency_key or ("local-" + uuid.uuid4().hex)
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO pending_parses(id,idempotency_key,request_json,state,created_at,updated_at)"
                " VALUES(?,?,?,'pending',?,?)",
                ("pp_" + uuid.uuid4().hex, key, json.dumps(dict(request), ensure_ascii=False, default=str), now, now),
            )
        return key

    def ready(self, limit: int = 10) -> list[dict[str, Any]]:
        now = self._clock()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pending_parses WHERE state IN ('pending','retry') AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
                (now, max(1, limit)),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "idempotency_key": row["idempotency_key"],
                "request": json.loads(row["request_json"]),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def mark_submitted(self, entry_id: str, job_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE pending_parses SET state='submitted',job_id=?,updated_at=? WHERE id=?",
                (job_id, self._clock(), entry_id),
            )

    def mark_retry(self, entry_id: str, error_code: str, delay: float = 30.0) -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE pending_parses SET state='retry',attempts=attempts+1,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                (error_code, now + max(5.0, delay), now, entry_id),
            )

    def mark_done(self, entry_id: str, state: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE pending_parses SET state=?,updated_at=? WHERE id=?", (state, self._clock(), entry_id)
            )

    def outstanding(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pending_parses WHERE state IN ('pending','retry') ORDER BY created_at"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "idempotency_key": row["idempotency_key"],
                "state": row["state"],
                "attempts": int(row["attempts"]),
                "last_error": row["last_error"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def outstanding_and_submitted(self) -> list[dict[str, Any]]:
        """Entries that already have a remote job and still need polling.

        These are kept until the remote outcome is known, so a merchant restart
        never loses the link between a local message and its cloud task.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pending_parses WHERE state='submitted' AND job_id IS NOT NULL ORDER BY created_at"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "idempotency_key": row["idempotency_key"],
                "job_id": row["job_id"],
                "message_id": self._message_id(row["request_json"]),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    @staticmethod
    def _message_id(request_json: str) -> str:
        try:
            payload = json.loads(request_json)
        except (TypeError, ValueError):
            return ""
        message = payload.get("message") if isinstance(payload, Mapping) else None
        return str(message.get("id", "")) if isinstance(message, Mapping) else ""

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ------------------------------------------------------------------ login flow


@dataclass
class LoginProgress:
    status: str
    detail: str = ""
    user_code: str = ""
    device_name: str = ""
    session: Session | None = None


class DeviceLoginFlow:
    """Drives one 登录 attempt: request, open browser, poll, persist."""

    def __init__(
        self,
        client: MerchantClient,
        credentials: CredentialStore,
        device_name: str,
        open_browser: bool = True,
        on_progress: Callable[[LoginProgress], None] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.credentials = credentials
        self.device_name = device_name
        self.open_browser = open_browser
        self.on_progress = on_progress
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    def _emit(self, status: str, detail: str = "", **kwargs: Any) -> None:
        if self.on_progress is not None:
            self.on_progress(LoginProgress(status=status, detail=detail, **kwargs))

    def run(self) -> Session:
        flow = self.client.start_login(self.device_name)
        user_code = flow["user_code"]
        interval = max(1.0, float(flow.get("interval", 5)))
        expires_in = max(1, int(flow.get("expires_in", 600)))
        self._emit(
            STATUS_WAITING_BROWSER,
            detail=f"请在浏览器中输入验证码 {user_code}",
            user_code=user_code,
            device_name=self.device_name,
        )
        if self.open_browser:
            url = flow.get("verification_uri_complete") or flow["verification_uri"]
            if not self._open_browser(url):
                # Browser could not be launched: the merchant can still paste the
                # code manually, so this is a hint, not a failure.
                self._emit(STATUS_WAITING_BROWSER, detail=f"请手动打开 {flow['verification_uri']} 并输入验证码 {user_code}")

        deadline = self._clock() + expires_in
        slow_until = 0.0
        while self._clock() < deadline:
            if self._stop.is_set():
                raise LoginError("已取消登录")
            self._sleep(interval)
            if self._clock() < slow_until:
                continue
            try:
                result = self.client.poll_login(flow["device_code"])
            except GatewayError as exc:
                if exc.code == "network_unavailable":
                    self._emit(STATUS_NETWORK_DOWN, detail="网络暂不可用，正在重试")
                    slow_until = self._clock() + 30
                    continue
                raise LoginError(exc.merchant_message) from exc
            state = result.get("status")
            if state == "pending":
                continue
            if state == "slow_down":
                interval += 2
                continue
            if state == "expired":
                raise LoginError("验证码已过期，请重新点击登录")
            if state == "denied":
                raise LoginError("您在网页上取消了本次登录")
            if state == "claimed":
                raise LoginError("本次登录已在其他窗口完成，请重新点击登录")
            if state != "approved":
                raise LoginError("登录未完成，请重新点击登录")
            session = self._build_session(result)
            self.credentials.save({"session": _session_to_dict(session)})
            self._emit(STATUS_LOGGED_IN, detail=f"欢迎，{session.merchant_name}", session=session)
            return session
        raise LoginError("验证码已过期，请重新点击登录")

    def _open_browser(self, url: str) -> bool:
        try:
            return bool(webbrowser.open(url))
        except Exception:  # pragma: no cover - platform dependent
            return False

    def _build_session(self, result: Mapping[str, Any]) -> Session:
        tokens = result["tokens"]
        account = result.get("account", {})
        device = result.get("device", {})
        session = Session(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            device_id=str(device.get("id", "")),
            merchant_name=str(account.get("display_name", "")),
            device_name=str(device.get("name", self.device_name)),
        )
        session.expires_at = self._clock() + float(tokens.get("expires_in", 900))
        try:
            session.subscription = self.client.__class__(
                self.client.base_url, session=session, timeout=self.client.timeout
            ).account()["subscription"]
        except GatewayError:
            session.subscription = {}
        return session


def _session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "device_id": session.device_id,
        "merchant_name": session.merchant_name,
        "device_name": session.device_name,
        "subscription": session.subscription,
        "expires_at": session.expires_at,
        "provider": session.provider,
        "gateway_url": session.gateway_url,
        "auth_origin": session.auth_origin,
    }


def restore_session(credentials: CredentialStore) -> Session | None:
    payload = credentials.load()
    if not payload or "session" not in payload:
        return None
    data = payload["session"]
    if not isinstance(data, Mapping):
        return None
    return Session(
        access_token=str(data.get("access_token", "")),
        refresh_token=str(data.get("refresh_token", "")),
        device_id=str(data.get("device_id", "")),
        merchant_name=str(data.get("merchant_name", "")),
        device_name=str(data.get("device_name", "")),
        subscription=dict(data.get("subscription") or {}),
        expires_at=float(data.get("expires_at", 0.0)),
        provider=str(data.get("provider", "product")),
        gateway_url=str(data.get("gateway_url", "")),
        auth_origin=str(data.get("auth_origin", "")),
    )


def clear_session(credentials: CredentialStore) -> None:
    credentials.clear()


def build_verify_url(base_url: str, user_code: str) -> str:
    """Fallback link when the merchant has to type the code by hand."""
    return f"{base_url.rstrip('/')}/device?{urlencode({'code': user_code})}"


# ------------------------------------------------------- local order context


def build_parse_request(
    message: Mapping[str, Any],
    customer: Mapping[str, Any],
    products,
    recent_messages=(),
    customer_facts=(),
    recent_orders=(),
    order_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the minimum context the cloud needs for one parse.

    This function is the data-minimisation boundary: long-term customer memory
    lives in the merchant's local :class:`~pyweixin.orders.OrderStore`, and only
    the facts chosen here ever leave the machine.
    """
    return {
        "message": {
            "id": str(message.get("id", "")),
            "sender": str(message.get("sender", "") or ""),
            "content": str(message.get("content", "")),
            "kind": str(message.get("kind", "text")),
            "sent_at": str(message.get("sent_at", "") or ""),
        },
        "customer": {
            "id": str(customer.get("id", "")),
            "name": str(customer.get("name", "") or ""),
            "alias": str(customer.get("alias", "") or ""),
            "address": str(customer.get("address", "") or ""),
        },
        "products": [dict(product) for product in products],
        "history": {
            "recent_messages": [dict(item) for item in recent_messages],
            "customer_facts": [dict(item) for item in customer_facts],
            "recent_orders": [dict(item) for item in recent_orders],
        },
        "order_state": dict(order_state or {}),
    }
