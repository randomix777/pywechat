"""Product gateway: browser device verification, product API and the job worker.

Two responsibilities live here and nowhere else:

* the merchant-facing HTTP surface (web pages + the local client API), and
* a single-threaded database-backed worker that drains the parse queue.

The gateway never exposes generic OpenCode management endpoints, never lets a
client choose a system prompt / tool / path / upstream address, and never trusts
a tenant id from a request body. Merchant identity always comes from the bearer
token resolved server-side.
"""
from __future__ import annotations

import json
import logging
import hmac
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .capability_check import run_order_capability_check
from .client_login import GatewayError
from .model_catalog import CatalogStatus, reject_client_model_override
from .pricing import ModelPrice, settle_micro_usd_for


def _optional_price(snapshot: Mapping[str, Any], key: str) -> float | None:
    """从策略里取一个可选单价。

    取不到、不是数字、或者 <= 0，一律返回 ``None`` —— 调用方据此退回固定额度。
    绝不用 0 顶替：0 会被当成"这个模型免费"，而免费档位已经焊死，
    那等于白送商户一次解析。
    """
    try:
        value = float(snapshot.get(key))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None

from .opencode import (
    AdapterError,
    AdapterRejected,
    AdapterTimeout,
    AdapterUnavailable,
    InvalidParseOutput,
    LocalRuleAdapter,
    OpenCodeAdapter,
    OutcomeUnknown,
    build_agent_prompt,
    extract_json_object,
    validate_parse_result,
)
from .product import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_OUTCOME_UNKNOWN,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    Conflict,
    Forbidden,
    IdempotencyConflict,
    ModelUnavailable,
    NotFound,
    PlatformBudgetExhausted,
    PricingChanged,
    PricingVersionRequired,
    ProductError,
    ProductStore,
    QuotaExhausted,
    Unauthenticated,
    USER_CODE_ATTEMPT_LIMIT,
    USER_CODE_ATTEMPT_WINDOW,
    ValidationFailed,
    utc_now,
)
from .webui import (
    DEVICE_PAGE_HTML,
    LOGIN_PAGE_HTML,
    SETTINGS_PAGE_HTML,
    page_shell,
    render_first_login,
)

LOGGER = logging.getLogger("pyweixin.gateway")

SESSION_COOKIE = "pyw_session"
MAX_BODY_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_ITEMS = 40
DEFAULT_COST_MICRO_USD = 1
#: Wall-clock ceiling for one parse job.
#:
#: Raised from 120s after measuring a real model: a failing upstream call took
#: 64-75s to surface its error (the provider retries internally before giving
#: up), and a working large-context model routinely runs longer than 60s. The
#: previous default expired *before* the provider had finished retrying, so the
#: gateway reported a timeout for work the model was still doing -- and the job
#: then sat unsettled, holding a concurrency slot.
JOB_TIMEOUT_SECONDS = 300.0
DEFAULT_JOB_WORKERS = 1


@dataclass
class GatewayResponse:
    status: int = 200
    payload: dict[str, Any] = field(default_factory=dict)
    html: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    raw: bytes = b""
    content_type: str = "application/json; charset=utf-8"

    def with_cookie(self, name: str, value: str, max_age: int, secure: bool) -> "GatewayResponse":
        parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if secure:
            parts.append("Secure")
        self.headers.append(("Set-Cookie", "; ".join(parts)))
        return self

    def clear_cookie(self, name: str, secure: bool) -> "GatewayResponse":
        parts = [f"{name}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
        if secure:
            parts.append("Secure")
        self.headers.append(("Set-Cookie", "; ".join(parts)))
        return self


class ProductGateway:
    """Route table, request validation and the parse worker."""

    def __init__(
        self,
        store: ProductStore,
        adapter: Any = None,
        public_base_url: str = "",
        secure_cookies: bool = True,
        job_timeout: float = JOB_TIMEOUT_SECONDS,
        max_body_bytes: int = MAX_BODY_BYTES,
        session_retention_seconds: float = 86400.0,
        adapter_report: Mapping[str, Any] | None = None,
        operator_token: str = "",
        catalog: Any = None,
        platform_budget: Any = None,
    ):
        self.store = store
        self.adapter = adapter if adapter is not None else LocalRuleAdapter()
        self.public_base_url = public_base_url.rstrip("/")
        self.secure_cookies = secure_cookies
        self.job_timeout = float(job_timeout)
        self.max_body_bytes = int(max_body_bytes)
        self.session_retention_seconds = float(session_retention_seconds)
        self.adapter_report = dict(adapter_report or {})
        self.operator_token = operator_token
        #: 模型目录。没有目录就没有可选模型 —— **不会**退回固定模型，
        #: 更不会退回 FakeBackend 冒充免费模型。
        self.catalog = catalog if catalog is not None else _empty_catalog()
        #: 平台自己的总预算 / 全局并发。None = 不限制。
        #: 传进来的是配置时也要**自动接上账本** —— 裸配置对象没有用量数据，
        #: 它的 ``can_accept`` 恒为真，预算保护会静默失效。
        self.platform_budget = _budget_with_ledger(store, platform_budget)
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._sessions_to_clean: list[str] = []
        self._lock = threading.RLock()
        #: 目录/计费策略的写锁。**校验计费版本、冻结快照、预留额度**这三步
        #: 必须在同一把锁里，否则会出现"按旧价确认、按新价扣费"，或者
        #: "校验时是免费模型、预留时已经变成收费"。所有会改目录的入口
        #: （刷新、验证、改策略）拿的都是这把锁。
        self._catalog_lock = threading.RLock()

    # ------------------------------------------------------------- lifecycle

    def start(self, workers: int = DEFAULT_JOB_WORKERS) -> None:
        # Allow a gateway instance to be started again after stop(); the worker
        # threads must observe a clear event or they exit immediately.
        self._stop.clear()
        recovered = self.store.recover_inflight_jobs()
        if any(recovered.values()):
            LOGGER.warning("recovered in-flight parse jobs after restart: %s", recovered)
        for index in range(max(1, int(workers))):
            thread = threading.Thread(target=self._worker_loop, name=f"parse-worker-{index}", daemon=True)
            thread.start()
            self._workers.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._workers:
            thread.join(timeout=timeout)
        self._workers.clear()

    # -------------------------------------------------------------- routing

    def owns(self, method: str, path: str) -> bool:
        if path in {"/", "/login", "/device", "/settings"} and method == "GET":
            return True
        if path in {"/healthz", "/v1/health"}:
            return True
        return path.startswith("/v1/")

    def handle(
        self,
        method: str,
        raw_path: str,
        headers: Mapping[str, str],
        body: bytes,
        read_json: Callable[[bytes], dict[str, Any]] | None = None,
    ) -> GatewayResponse:
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        cookies = _parse_cookies(_header(headers, "Cookie"))
        try:
            if method == "GET":
                return self._handle_get(path, query, headers, cookies)
            if method == "POST":
                payload = read_json(body) if read_json is not None else _decode_json(body)
                return self._handle_post(path, payload, headers, cookies)
            if method == "PATCH":
                payload = read_json(body) if read_json is not None else _decode_json(body)
                return self.handle_patch(path, payload, headers, cookies)
            return _json(405, {"error": "method_not_allowed"})
        except (PricingChanged, PlatformBudgetExhausted, PricingVersionRequired) as exc:
            # 这三个不是"拒绝"，是"换个说法再来"：客户端需要拿到新的计费事实
            # 重新展示给商户确认，所以把 facts 一起回传。
            return _json(exc.http_status, {"error": exc.code, "message": str(exc), "billing": exc.facts})
        except ProductError as exc:
            return _json(exc.http_status, {"error": exc.code, "message": str(exc)})
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid_json"})
        except Exception:  # pragma: no cover - never leak internals to clients
            LOGGER.exception("unhandled gateway error on %s %s", method, path)
            return _json(500, {"error": "internal_error", "message": "服务暂时不可用，请稍后再试"})

    def decode_body(self, body: bytes) -> dict[str, Any]:
        if len(body) > self.max_body_bytes:
            raise ValidationFailed("提交内容过大", code="payload_too_large")
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationFailed("请求格式不正确", code="invalid_json") from exc
        if not isinstance(payload, dict):
            raise ValidationFailed("请求必须是 JSON 对象", code="invalid_request")
        return payload

    # ------------------------------------------------------------------- GET

    def _handle_get(
        self, path: str, query: Mapping[str, list[str]], headers: Mapping[str, str], cookies: Mapping[str, str]
    ) -> GatewayResponse:
        if path in {"/healthz", "/v1/health"}:
            return self._health()
        if path in {"/", "/login"}:
            return _html(200, LOGIN_PAGE_HTML)
        if path == "/settings":
            session = self.store.load_web_session(cookies.get(SESSION_COOKIE, ""))
            if session is None:
                return _html(200, page_shell())
            return _html(200, SETTINGS_PAGE_HTML)
        if path == "/device":
            session = self.store.load_web_session(cookies.get(SESSION_COOKIE, ""))
            if session is None:
                return _html(200, page_shell())
            if session.get("must_change_password"):
                return _html(200, render_first_login(session["display_name"], self.store.csrf_token(session["id"])))
            return self._device_page(query, session, cookies)
        if path == "/v1/account":
            return _json(200, self._account_payload(self._require_client(headers)))
        if path == "/v1/devices":
            identity = self._require_client(headers)
            return _json(200, {"devices": self.store.list_devices(identity["account_id"])})
        if path == "/v1/subscription":
            identity = self._require_client(headers)
            # 进度条靠的是 subscription 里的 quota_micro_usd / used_micro_usd，
            # 那些已经在这里。**不下发单价**：
            # 商户要的是百分比，不是「还够做多少次」
            # （一条消息并不等于一笔订单）。
            return _json(200, {"subscription": self.store.get_subscription(identity["account_id"])})
        if path == "/v1/models":
            return self._models_response(self._optional_identity(headers))
        if path == "/v1/models/refresh":
            # 客户主动刷新：只标记需要刷新，实际拉取由缓存周期完成，
            # 避免每个客户端点一下就打上游一次。
            return self._models_response(self._optional_identity(headers))
        if path == "/v1/order-parse-jobs":
            identity = self._require_client(headers)
            states = [state for state in query.get("state", [""])[0].split(",") if state]
            return _json(200, {"jobs": self.store.list_jobs(identity["account_id"], states, limit=50)})
        match = re.fullmatch(r"/v1/order-parse-jobs/([A-Za-z0-9_.-]{1,64})", path)
        if match:
            identity = self._require_client(headers)
            job = self.store.get_job(identity["account_id"], match.group(1))
            # 计费字段随任务一起下发：客户端只**展示**这里的数字，不自己算。
            return _json(200, {"job": job, "billing": self._billing_payload(identity, match.group(1))})
        if path == "/v1/operator/jobs":
            self._require_operator(headers)
            states = [state for state in query.get("state", [""])[0].split(",") if state]
            return _json(200, {"jobs": self.store.list_all_jobs(states, limit=100)})
        if path == "/v1/operator/models":
            self._require_operator(headers)
            return _json(200, self._operator_models())
        if path == "/v1/operator/jobs/{job_id}/reconcile":
            self._require_operator(headers)
            match = re.fullmatch(r"/v1/operator/jobs/([A-Za-z0-9_.-]{1,64})/reconcile", path)
            if not match:
                raise NotFound("任务不存在")
            return _json(200, self.reconcile_job(match.group(1)))
        if path == "/v1/operator/usage":
            self._require_operator(headers)
            return _json(200, {**self.store.operator_usage_summary(), "platform_budget": self.platform_budget_status()})
        if path == "/v1/operator/execution-backend":
            self._require_operator(headers)
            return _json(200, self.execution_backend_report())
        return _json(404, {"error": "not_found"})

    # ------------------------------------------------------------------ POST

    def _handle_post(
        self, path: str, payload: Mapping[str, Any], headers: Mapping[str, str], cookies: Mapping[str, str]
    ) -> GatewayResponse:
        # 目录相关的三个运营端点是**会改状态**的：刷新会重拉上游、验证会发真实
        # 请求、改策略会换默认模型。它们只能走 POST —— 之前被错挂在 GET 里，
        # 实际上永远调不通。
        if path in {"/v1/operator/models/refresh", "/v1/operator/models/verify", "/v1/operator/models/policy"}:
            self._require_operator(headers)
            if path.endswith("/refresh"):
                return _json(200, self._operator_refresh_catalog(payload))
            if path.endswith("/verify"):
                return _json(200, self._operator_verify_model(payload))
            return _json(200, self._operator_set_policies(payload))
        if path == "/v1/device/authorize":
            device_name = str(payload.get("device_name", "")).strip()
            platform = str(payload.get("platform", "")).strip()[:64]
            flow = self.store.start_device_authorization(device_name, platform)
            verification = f"{self.public_base_url}/device" if self.public_base_url else "/device"
            return _json(
                201,
                {
                    "device_code": flow["device_code"],
                    "user_code": flow["user_code"],
                    "verification_uri": verification,
                    "verification_uri_complete": f"{verification}?code={flow['user_code']}",
                    "expires_in": flow["expires_in"],
                    "interval": flow["interval"],
                },
            )
        if path == "/v1/device/authorize/poll":
            result = self.store.poll_device_authorization(str(payload.get("device_code", "")))
            return _json(200, result)
        if path == "/v1/auth/refresh":
            refreshed = self.store.refresh_access_token(str(payload.get("refresh_token", "")))
            return _json(200, refreshed)
        if path == "/v1/auth/logout":
            identity = self._require_client(headers)
            del identity
            self.store.revoke_refresh_token(str(payload.get("refresh_token", "")))
            return _json(200, {"ok": True})
        if path == "/v1/order-parse-jobs":
            identity = self._require_client(headers)
            return self._create_job(identity, payload, headers)
        match = re.fullmatch(r"/v1/order-parse-jobs/([A-Za-z0-9_.-]{1,64})/cancel", path)
        if match:
            identity = self._require_client(headers)
            job = self.store.request_cancel(identity["account_id"], match.group(1))
            return _json(200, {"job": job})
        if path == "/v1/web/login":
            return self._web_login(payload, headers)
        if path == "/v1/web/logout":
            session = self.store.load_web_session(cookies.get(SESSION_COOKIE, ""))
            if session is not None:
                self.store.revoke_web_session(session["id"])
            return _json(200, {"ok": True}).clear_cookie(SESSION_COOKIE, self.secure_cookies)
        if path == "/v1/web/device/authorize":
            return self._web_authorize(payload, headers, cookies)
        if path == "/v1/web/password/change":
            return self._web_change_password(payload, headers, cookies)
        if path == "/v1/web/logout-all":
            session = self._require_session(headers, cookies)
            self.store.logout_everywhere(session["account_id"])
            return _json(200, {"ok": True}).clear_cookie(SESSION_COOKIE, self.secure_cookies)
        if re.fullmatch(r"/v1/devices/[A-Za-z0-9_.-]{1,64}/revoke", path):
            identity = self._require_client(headers)
            device_id = path.split("/")[3]
            return _json(200, {"device": self.store.revoke_device(identity["account_id"], device_id)})
        if re.fullmatch(r"/v1/operator/accounts", path):
            self._require_operator(headers)
            return _json(201, self._operator_create_account(payload))
        match = re.fullmatch(r"/v1/operator/accounts/([A-Za-z0-9_.-]{1,64})", path)
        if match:
            self._require_operator(headers)
            return _json(200, self._operator_update_account(match.group(1), payload))
        if re.fullmatch(r"/v1/operator/jobs/([A-Za-z0-9_.-]{1,64})/reconcile", path):
            self._require_operator(headers)
            return _json(200, {"job": self.reconcile_job(path.split("/")[4])})
        return _json(404, {"error": "not_found"})

    def handle_patch(
        self, path: str, payload: Mapping[str, Any], headers: Mapping[str, str], cookies: Mapping[str, str]
    ) -> GatewayResponse:
        del cookies
        match = re.fullmatch(r"/v1/operator/accounts/([A-Za-z0-9_.-]{1,64})", path)
        if match:
            self._require_operator(headers)
            return _json(200, self._operator_update_account(match.group(1), payload))
        match = re.fullmatch(r"/v1/devices/([A-Za-z0-9_.-]{1,64})", path)
        if match:
            identity = self._require_client(headers)
            return _json(200, {"device": self.store.rename_device(identity["account_id"], match.group(1), str(payload.get("name", "")))})
        return _json(404, {"error": "not_found"})

    # ------------------------------------------------------------- web pages

    def _device_page(self, query: Mapping[str, list[str]], session: Mapping[str, Any], cookies: Mapping[str, str]) -> GatewayResponse:
        code = (query.get("code", [""])[0] or "").strip().upper()
        pending = None
        if code:
            row = self.store.find_authorization_by_user_code(code)
            if row is not None and row["status"] in {"pending", "approved"}:
                pending = row
        return _html(
            200,
            DEVICE_PAGE_HTML.render(
                merchant=session["display_name"],
                user_code=code,
                pending=pending,
                csrf=self.store.csrf_token(session["id"]),
            ),
        )

    def _web_login(self, payload: Mapping[str, Any], headers: Mapping[str, str]) -> GatewayResponse:
        self.store.rate_limit(f"web-login:{_client_ip(headers)}", limit=15, window=300.0, code="login_rate_limited")
        account = self.store.authenticate_password(str(payload.get("username", "")), str(payload.get("password", "")))
        session_id, csrf, _ = self.store.create_web_session(account["id"])
        response = _json(200, {"ok": True, "display_name": account["display_name"], "must_change_password": bool(account["must_change_password"])})
        response.headers.append(
            ("Set-Cookie", f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200" + ("; Secure" if self.secure_cookies else ""))
        )
        response.headers.append(("X-CSRF-Token", csrf))
        return response

    def _web_authorize(self, payload: Mapping[str, Any], headers: Mapping[str, str], cookies: Mapping[str, str]) -> GatewayResponse:
        session = self._require_session(headers, cookies)
        if not self.store.check_csrf(session, str(_header(headers, "X-CSRF-Token")) or str(payload.get("csrf_token", ""))):
            raise Forbidden("页面已过期，请刷新后重试", code="csrf_failed")
        # The user_code is short by design, so guessing must be throttled per
        # account and per browser before any lookup happens.
        self.store.rate_limit(
            f"device-code-guess:{session['account_id']}",
            limit=USER_CODE_ATTEMPT_LIMIT,
            window=USER_CODE_ATTEMPT_WINDOW,
            code="device_code_rate_limited",
        )
        self.store.rate_limit(
            f"device-code-guess-ip:{_client_ip(headers)}",
            limit=USER_CODE_ATTEMPT_LIMIT * 2,
            window=USER_CODE_ATTEMPT_WINDOW,
            code="device_code_rate_limited",
        )
        user_code = str(payload.get("user_code", ""))
        action = str(payload.get("action", "approve"))
        if action == "deny":
            return _json(200, {"ok": True, **self.store.deny_device_authorization(user_code)})
        return _json(200, {"ok": True, **self.store.approve_device_authorization(user_code, session["account_id"])})

    def _web_change_password(self, payload: Mapping[str, Any], headers: Mapping[str, str], cookies: Mapping[str, str]) -> GatewayResponse:
        session = self._require_session(headers, cookies)
        if not self.store.check_csrf(session, str(_header(headers, "X-CSRF-Token")) or str(payload.get("csrf_token", ""))):
            raise Forbidden("页面已过期，请刷新后重试", code="csrf_failed")
        new_password = str(payload.get("new_password", ""))
        if len(new_password) < 8:
            raise ValidationFailed("新密码至少 8 位", code="weak_password")
        self.store.change_password(session["account_id"], str(payload.get("current_password", "")), new_password)
        return _json(200, {"ok": True, "message": "密码已修改，请重新登录电脑上的软件"})

    def _optional_identity(self, headers: Mapping[str, str]) -> Any:
        """能认证就返回身份，否则 ``None``。

        模型目录允许匿名查看 —— 商户要在登录前就知道有哪些模型可选 ——
        但免费额度统计只对已认证的账号有意义。
        """
        authorization = _header(headers, "Authorization")
        if not authorization.lower().startswith("bearer "):
            return None
        try:
            return self.store.authenticate_access_token(authorization[7:].strip())
        except Exception:
            return None

    def _models_response(self, identity: Any) -> GatewayResponse:
        """客户可见的模型目录。

        只有别名。provider / model id / 上游价格 / 地址 / 凭据一律不出现。
        拉取失败时返回**最近一次成功**的缓存，并明确标记它不是最新。
        """
        self.catalog.refresh()
        payload = self.catalog.for_client()
        return _json(200, {"catalog": payload})

    def _operator_refresh_catalog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """运营手动拉一次上游目录。"""
        with self._catalog_lock:
            self.catalog.refresh(force=True)
            self.store.save_catalog(self.catalog.catalog)
        return {
            "version": self.catalog.catalog.version,
            "updated_at": self.catalog.catalog.updated_at,
            "fetched_ok": self.catalog.catalog.fetched_ok,
            "entries": len(self.catalog.catalog),
            "discovered": sorted(
                alias for alias, entry in self.catalog.catalog.entries.items()
                if entry.status == "discovered"
            ),
        }

    def _operator_verify_model(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """按需验证一个模型的订单能力。

        用**合成数据**发真实请求，绝不上传客户聊天。验证通过才允许它成为
        可选/默认免费模型。少量通过只算准入冒烟，不代表生产准确率达标。
        """
        alias = str(payload.get("alias", "")).strip()
        entry = self.catalog.catalog.entry(alias)
        if entry is None:
            raise NotFound("模型别名不存在")
        # 验证期间目录不变：否则"验证的模型"和"之后被冻结的模型"可能不是
        # 同一个，结论就成了假通过。
        with self._catalog_lock:
            checks = run_order_capability_check(
                self.adapter,
                provider_id=entry.provider_id,
                model_id=entry.model_id,
                max_cases=int(payload.get("max_cases", 0) or 0),
            )
            passed = bool(checks.get("passed"))
            self.store.record_model_verification(
                alias,
                backend=str(getattr(self.adapter, "name", "")),
                provider_id=entry.provider_id,
                model_id=entry.model_id,
                prompt_version=str(getattr(self.adapter, "prompt_version", "")),
                passed=passed,
                checks=checks,
            )
            now = utc_now()
            self.catalog.mark_status(
                alias,
                CatalogStatus.SELECTABLE if passed else CatalogStatus.UNAVAILABLE,
                reason="" if passed else "未通过订单能力验证",
                verified=passed,
                verification=checks,
                now=now,
            )
            self.store.save_catalog(self.catalog.catalog)
        return {
            "alias": alias,
            "passed": passed,
            "checks": checks,
            "verified_at": now,
            "note": "冒烟验证通过不等于生产准确率已达标",
        }

    def _operator_set_policies(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """运营配置允许列表、免费标记与各项上限。"""
        from .model_catalog import OperatorModelPolicy

        items = payload.get("models")
        if not isinstance(items, list) or not items:
            raise ValidationFailed("models 必须是模型策略列表", code="bad_models")
        policies = [OperatorModelPolicy.from_dict(item) for item in items if isinstance(item, Mapping)]
        if len({policy.alias for policy in policies}) != len(policies):
            raise ValidationFailed("模型别名不能重复", code="duplicate_alias")
        defaults = [p.alias for p in policies if p.is_default]
        if len(defaults) > 1:
            # 旧版只拦“多个默认**免费**模型”，改成多个默认模型
            # 不会被拦，而会让默认选择变得不明确。
            raise ValidationFailed("只能有一个默认模型", code="multiple_defaults")
        # 改策略会改 ``catalog.version``，也就是商户确认过的那一版。必须与
        # 提交路径拿同一把锁，否则"改策略"和"校验版本"会交错。
        with self._catalog_lock:
            self.catalog.load_policies(policies)
            self.store.save_model_policies(policies)
            self.catalog.refresh(force=True)
            self.store.save_catalog(self.catalog.catalog)
        return {
            "version": self.catalog.catalog.version,
            "policies": [p.to_dict() for p in policies],
            "default_alias": self.catalog.catalog.default_entry().alias if self.catalog.catalog.default_entry() else "",
        }

    def _operator_models(self) -> dict[str, Any]:
        """运营视图：可以看到真实 provider/model，但只在这里。"""
        catalog = self.catalog.catalog
        return {
            "version": catalog.version,
            "updated_at": catalog.updated_at,
            "fetched_ok": catalog.fetched_ok,
            "entries": {
                alias: {
                    "alias": entry.alias,
                    "display_name": entry.display_name,
                    "provider_id": entry.provider_id,
                    "model_id": entry.model_id,
                    "status": entry.status,
                    "verified_at": entry.verified_at,
                    "verification": dict(entry.verification),
                    "upstream_cost": dict(entry.upstream_cost),
                    "context_limit": entry.context_limit,
                }
                for alias, entry in catalog.entries.items()
            },
            "policies": [p.to_dict() for p in self.catalog.policies()],
        }

    # ------------------------------------------------------------------ jobs

    def _create_job(
        self, identity: Mapping[str, Any], payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> GatewayResponse:
        idempotency_key = str(_header(headers, "Idempotency-Key") or payload.get("idempotency_key", "")).strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValidationFailed("缺少幂等键（Idempotency-Key）", code="missing_idempotency_key")
        request = self._validate_job_request(payload)
        # 客户只能提交产品别名。provider / model id / 地址 / 密钥一律拒绝。
        reject_client_model_override(request)
        request["model"] = str(request.get("model") or "")
        # 哈希里已经包含别名，所以"同一个键换了模型"和"换了内容"一样会被
        # 判为冲突 —— 换模型必须显式新建任务，不能静默改，也不能借幂等机制
        # 换模型。
        request_hash = ProductStore.request_hash(request)

        # ── 幂等重放：先认旧账，再谈价格 ──────────────────────────────
        # 同一个键 + 同一份业务请求 = 同一个任务。返回**原任务与原快照**，
        # 绝不按当前价格重新收费。价格后来变了也不能改写已冻结的计费。
        replay = self._idempotent_replay(identity, idempotency_key, request_hash)
        if replay is not None:
            return replay

        # ── 校验、冻结、预留必须是一个不可分割的临界区 ────────────────
        # 目录/策略在这三步之间变化，会出现"按旧价确认、按新价扣费"或
        # "校验时免费、预留时收费"。所以整段在目录锁里做，而所有会改目录的
        # 入口（刷新、验证、改策略）拿的是同一把锁。
        with self._catalog_lock:
            routing = self._resolve_model(request.get("model"), identity)
            # 计费版本校验。收费任务**必须**带有效版本；免费转收费也不能
            # 靠"没带版本"蒙混过去。
            self._check_pricing_version(payload, routing)
            request["model"] = routing["alias"]
            job = self.store.create_job(
                identity["account_id"],
                identity["device_id"],
                identity["merchant_id"],
                idempotency_key,
                request_hash,
                routing["cost_micro_usd"],
                min(self.job_timeout, routing["max_task_seconds"]) if routing["max_task_seconds"] else self.job_timeout,
                request_payload=request,
                max_concurrent=routing["max_concurrent_per_tenant"],
                platform_budget=self.platform_budget,
                snapshot=self._job_snapshot(routing),
            )
        # 兜底：极老的数据（快照早于本机制）仍能补写，但不覆盖已有快照 ——
        # 幂等重放时覆盖它会让历史对不上。
        if job.get("created_at") and not self.store.job_snapshot(job["job_id"]):
            frozen = self._job_snapshot(routing)
            self.store.save_job_snapshot(
                job["job_id"],
                frozen["alias"], frozen["provider_id"], frozen["model_id"],
                frozen["catalog_version"], frozen["pricing_class"], frozen["pricing_version"],
                0, frozen["prompt_version"], frozen["plugin_version"],
                extra=frozen["snapshot"],
            )
        status = 200 if job["state"] in {"queued", "running", "succeeded"} else 202
        return _json(status, {
            "job": job,
            "subscription": self.store.get_subscription(identity["account_id"]),
            # 这是**唯一**的计费事实来源。客户端拿到它就原样显示，不再自己
            # 按"每个任务扣 1 次"推算。
            "billing": self._billing_payload(identity, job["job_id"]),
        })

    def _idempotent_replay(
        self, identity: Mapping[str, Any], idempotency_key: str, request_hash: str
    ) -> GatewayResponse | None:
        """同键同请求 ⇒ 返回原任务与原计费快照；同键不同请求 ⇒ 冲突。

        放在价格校验**之前**：一笔已经按旧价确认并预留过的任务，事后价格变了
        也不能被重新收费。价格变化只影响**新**任务。
        """
        existing = self.store.find_job_by_idempotency(identity["account_id"], idempotency_key)
        if existing is None:
            return None
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflict("幂等键已用于不同的解析内容", code="idempotency_key_reuse")
        job = self.store.get_job(identity["account_id"], existing["job_id"])
        # 播报冻结当时的口径，而不是当前价格：商户看到的是他当初同意的那一版。
        return _json(200, {
            "job": job,
            "subscription": self.store.get_subscription(identity["account_id"]),
            "billing": self._billing_payload(identity, existing["job_id"]),
            "replayed": True,
        })

    def _billing_payload(self, identity: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        """这个任务的机器可读计费事实。

        免费与收费的实际扣减、预留与结算都在服务端算完了；这里把它读出来下发，
        附带**冻结的策略版本**，客户端据此生成一致文案并在策略变化时要求重新
        确认。``charge_line`` 是给商户看的一句话，不是计算依据。
        """
        account_id = identity["account_id"]
        row = self.store.job_row(job_id)
        if row is None or str(row["account_id"]) != str(account_id):
            return {"available": False}
        snapshot = self.store.job_snapshot(job_id) or {}
        frozen = dict(snapshot.get("snapshot") or {})
        subscription = self.store.get_subscription(account_id)
        entry = self.catalog.entry(str(snapshot.get("alias") or ""))
        return {
            "available": True,
            "job_id": job_id,
            "state": str(row["state"]),
            "model_alias": str(snapshot.get("alias") or ""),
            "pricing_class": str(snapshot.get("pricing_class") or ""),
            # 已结算的扣费。排队/执行中为 0，预留在 charged_micro_usd 之前记。
            "charged_micro_usd": int(row["charged_micro_usd"]),
            "reserved_micro_usd": int(row["reserved_micro_usd"]),
            "cost_micro_usd_per_job": int(frozen.get("cost_micro_usd_per_job") or 0),
            "charge_line": entry.charge_line() if entry is not None else "",
            "subscription_remaining_units": int(subscription.get("remaining_micro_usd", 0)),
            # 策略变了客户端必须重新确认，不能让客户在不知情时承担更高收费。
            "policy_version": str(snapshot.get("pricing_version") or frozen.get("policy_version") or ""),
            "frozen_policy_version": str(frozen.get("policy_version") or ""),
        }

    def _job_snapshot(self, routing: Mapping[str, Any]) -> dict[str, Any]:
        """任务创建时冻结下来的路由与计费事实。

        客户之后换模型、目录刷新、上游下线，网络重试多少次，都不再影响它。
        """
        return {
            "alias": routing["alias"],
            "provider_id": routing["provider_id"],
            "model_id": routing["model_id"],
            "catalog_version": self.catalog.catalog.version,
            "pricing_class": routing["pricing_class"],
            "pricing_version": routing["pricing_version"],
            "prompt_version": routing["prompt_version"],
            "plugin_version": routing["plugin_version"],
            "snapshot": {
                # 不再冻结免费相关的东西。订阅额度是按周期的，
                # 不存在「跨午夜记回预留那天」的问题 —— 那只是免费额度的日号。
                "cost_micro_usd_per_job": routing["cost_micro_usd"],
                # **冻结单价**（美元/百万 token）。商户确认过的是这个价，
                # 所以运营事后改价不能影响已经在跑的任务 —— 否则商户确认过的
                # 和最后被扣的不是一回事，那是最伤信任的一种错。
                #
                # 没有单价时是 None 而不是 0：0 会被当成"免费"，而**免费档位
                # 已经焊死**，单价拿不到只能退回固定的 cost_micro_usd_per_job，
                # 不能悄悄按 0 结算。
                "input_price_per_million": self._frozen_price_of(routing),
                "output_price_per_million": self._frozen_output_price_of(routing),
                "max_concurrent_per_tenant": routing["max_concurrent_per_tenant"],
                "requests_per_minute": routing["requests_per_minute"],
                "max_task_seconds": routing["max_task_seconds"],
                "max_output_tokens": routing["max_output_tokens"],
                "policy_version": routing["policy_version"],
            },
        }

    def _frozen_price_of(self, routing: Mapping[str, Any]) -> float | None:
        """冻结的输入单价，拿不到就返回 None（不编）。"""
        value = routing.get("input_price_per_million")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _frozen_output_price_of(self, routing: Mapping[str, Any]) -> float | None:
        """冻结的输出单价，拿不到就返回 None（不编）。"""
        value = routing.get("output_price_per_million")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _check_pricing_version(self, payload: Mapping[str, Any], routing: Mapping[str, Any]) -> None:
        """提交前校验计费策略版本。

        规则（三条，缺一不可）：

        * **收费任务必须带有效版本。** 没有版本就没有"商户确认过的那个价"，
          此时按当前价格扣就是不知情收费 —— 直接拒绝，不建任务、不预留、
          不执行。返回 ``pricing_version_required`` 与当前事实，让客户端先
          展示金额、让商户确认。
        * **带了版本就必须对得上。** 对不上说明价格在商户看过之后变了，退回
          ``pricing_changed`` 加最新事实，**要商户重新确认**，不自动确认。
          免费转收费也走这条：旧请求不能静默按新价继续。
        * 免费任务**不强制**带版本（它不扣订阅额度），但带了就必须对上。

        调用方必须持有 ``_catalog_lock``，否则"校验"与"冻结/预留"之间会有
        策略变化窗口。
        """
        presented = str(payload.get("pricing_version") or "").strip()
        current = str(routing["pricing_version"])
        entry = self.catalog.entry(str(routing["alias"]))
        facts = {
            "pricing_class": str(routing["pricing_class"]),
            "cost_micro_usd_per_job": int(routing["cost_micro_usd"]),
            "charge_line": entry.charge_line() if entry is not None else "",
            "pricing_version": current,
            "presented_version": presented,
        }
        # 每任务都收费。免费档位已被焊死，这里不再有「不收费」的分支。
        charged = True
        if not presented:
            if charged:
                raise PricingVersionRequired(
                    "提交前需要先确认这次分析的费用", code="pricing_version_required", facts=facts,
                )
            return
        if presented != current:
            raise PricingChanged(
                "分析价格或免费次数规则有变化，请确认后再提交",
                code="pricing_changed", facts=facts,
            )

    def _resolve_model(self, alias: Any, identity: Mapping[str, Any]) -> dict[str, Any]:
        """别名 → 真正要执行的 provider/model，并冻结计费。

        免费与收费在这里分流：免费任务预留**每日免费额度**，不碰订阅额度；
        收费任务照常走余额检查，不能绕过。

        无论走哪条，都先过**充值门禁**（见 :meth:`_remaining_analysis_units`）。
        """
        catalog = self.catalog
        default = catalog.default_entry()
        requested = str(alias or "").strip()
        if not requested:
            if default is None:
                # 没有合格免费模型就明说，不自动切收费模型。
                raise ModelUnavailable("暂时没有可用于分析消息的模型，请稍后重试或联系客服", code="no_free_model")
            entry = default
        else:
            try:
                entry = catalog.resolve(requested)   # 不可选时抛错，不静默替换
            except GatewayError as exc:
                # 目录层用的是客户端错误类型；网关必须翻译成自己的响应码，
                # 否则会变成 500，让商户以为是服务器坏了。
                raise ModelUnavailable(str(exc), code=exc.code) from exc
        policy = entry.policy
        billing = entry.billing_snapshot()
        # 余额检查**不在这里**。它在更靠后的预留阶段（``reserve_job_units``）
        # 统一做，那一处已经能区分两种完全不同的原因：
        #
        # * ``subscription_inactive`` 套餐到期 → 要**续费**
        # * ``quota_exhausted``        点数用完 → 要**充值**
        #
        # 早先在这里加过一道"余额为 0 就拒绝"的门禁，结果它把上面两个更准确的
        # 错误**遮住**了：套餐到期的用户被告知"请充值"，那是误导 —— 到期要续期，
        # 充值并不能让套餐重新生效。而且它和预留阶段的检查重复，属于两套口径。
        #
        # 关键点：预留发生在**任何模型调用之前**，所以余额为 0 时请求根本不会
        # 发到上游。这一条由 ``test_zero_balance_never_reaches_the_backend`` 钉住。
        # 以前这里有一整段「免费分流」：免费任务走每日免费额度，
        # 不碰订阅额度。那条路径已被焊死（见 ``OperatorModelPolicy.__post_init__``），
        # 所以里面的 ``pricing_class`` 永远是 ``metered``：任何分析都走订阅额度。
        return {
            "alias": entry.alias,
            "provider_id": entry.provider_id,
            "model_id": entry.model_id,
            "pricing_class": billing["pricing_class"],
            # 计费只认结构化策略里的数字，**不认** charge_note 之类的文案。
            "cost_micro_usd": int(billing["cost_micro_usd_per_job"]),
            # 单价（美元/百万 token）。运营在策略里显式配置；没配就是 None，
            # 结算时退回上面那个固定额度 —— **不猜价格**，也不按 0 结算
            # （0 等于免费，而免费档位已经焊死）。
            "input_price_per_million": _optional_price(billing, "input_price_per_million"),
            "output_price_per_million": _optional_price(billing, "output_price_per_million"),
            "max_concurrent_per_tenant": int(billing["max_concurrent_per_tenant"]),
            "requests_per_minute": int(billing["requests_per_minute"]),
            "policy_version": str(billing["policy_version"]),
            "max_task_seconds": float(billing["max_task_seconds"]),
            "max_output_tokens": int(billing["max_output_tokens"]),
            "pricing_version": catalog.version,
            "prompt_version": getattr(self.adapter, "prompt_version", "") or "",
            "plugin_version": getattr(self.adapter, "plugin_version", "") or "",
        }

    def _validate_job_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize and bound the client payload.

        The client may not choose a system prompt, tools, a filesystem path or
        an upstream address; only the fields listed here are accepted.
        """
        # ``model`` 不在禁止之列：客户提交的是**产品别名**，由网关解析成
        # 真正的 provider/model。provider_id / model_id / 上游地址仍然禁止。
        forbidden_keys = {"system_prompt", "tools", "prompt", "upstream", "upstream_url", "path",
                          "provider", "provider_id", "model_id", "base_url", "api_key", "token", "endpoint",
                          "agent", "tenant_id", "merchant_id"}
        present = forbidden_keys & {str(key) for key in payload}
        if present:
            raise ValidationFailed(f"请求包含不支持的字段：{', '.join(sorted(present))}", code="unsupported_field")
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ValidationFailed("缺少待解析消息", code="missing_message")
        content = str(message.get("content", ""))
        if not content.strip():
            raise ValidationFailed("消息内容为空", code="empty_message")
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValidationFailed(f"单条消息过长（上限 {MAX_MESSAGE_CHARS} 字）", code="message_too_long")
        kind = str(message.get("kind") or "text")
        if kind not in {"text", "text_message"}:
            raise ValidationFailed("暂只支持文字消息解析", code="unsupported_message_kind")
        customer = payload.get("customer")
        if not isinstance(customer, Mapping) or not str(customer.get("id", "")).strip():
            raise ValidationFailed("缺少已绑定的客户身份", code="missing_customer")
        products = payload.get("products")
        if not isinstance(products, list) or not products:
            raise ValidationFailed("缺少商品资料", code="missing_products")
        if len(products) > 500:
            raise ValidationFailed("商品资料过多", code="too_many_products")
        history = payload.get("history", {})
        if not isinstance(history, Mapping):
            raise ValidationFailed("history 必须是对象", code="bad_history")
        recent = history.get("recent_messages", [])
        facts = history.get("customer_facts", [])
        orders = history.get("recent_orders", [])
        for name, items, cap in (("recent_messages", recent, MAX_HISTORY_ITEMS), ("customer_facts", facts, 100), ("recent_orders", orders, 20)):
            if not isinstance(items, list):
                raise ValidationFailed(f"history.{name} 必须是列表", code="bad_history")
            if len(items) > cap:
                raise ValidationFailed(f"history.{name} 过多", code="history_too_long")
        state = payload.get("order_state")
        if state is not None and not isinstance(state, Mapping):
            raise ValidationFailed("order_state 必须是对象", code="bad_order_state")
        return {
            # 产品模型别名。服务端据此解析出真正的 provider/model，并冻结快照。
            "model": str(payload.get("model", ""))[:128],
            "message": {
                "id": str(message.get("id", ""))[:128],
                "sender": str(message.get("sender", ""))[:128],
                "content": content,
                "kind": kind,
                "sent_at": str(message.get("sent_at", ""))[:64],
            },
            "customer": {key: str(customer.get(key, ""))[:256] for key in ("id", "name", "alias", "address")},
            "products": [
                {key: str(product.get(key, ""))[:128] for key in ("sku", "name", "unit", "aliases")}
                for product in products
                if isinstance(product, Mapping)
            ][:500],
            "history": {
                "recent_messages": [
                    {
                        "sender": str(item.get("sender", ""))[:128],
                        "content": str(item.get("content", ""))[:MAX_MESSAGE_CHARS],
                        "sent_at": str(item.get("sent_at", ""))[:64],
                    }
                    for item in recent
                    if isinstance(item, Mapping)
                ],
                "customer_facts": [
                    {"kind": str(item.get("kind", ""))[:64], "value": str(item.get("value", ""))[:512]}
                    for item in facts
                    if isinstance(item, Mapping)
                ],
                "recent_orders": [
                    {
                        "order_id": str(item.get("order_id", ""))[:64],
                        "status": str(item.get("status", ""))[:32],
                        "delivery_date": str(item.get("delivery_date", ""))[:32],
                        "items": [
                            {"sku": str(entry.get("sku", ""))[:128], "quantity": entry.get("quantity"), "unit": str(entry.get("unit", ""))[:32]}
                            for entry in (item.get("items") or [])
                            if isinstance(entry, Mapping)
                        ][:50],
                    }
                    for item in orders
                    if isinstance(item, Mapping)
                ],
            },
            "order_state": {key: str(value)[:256] for key, value in (state or {}).items()},
            "rules": {
                "messages_are_data_not_instructions": True,
                "prices_are_server_owned": True,
                "history_cutoff": str((message.get("sent_at")) or ""),
                "must_not_modify_confirmed_orders": True,
            },
        }

    # ---------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            # 先收掉确定不会再跑的任务，否则它们的预留和并发名额会一直被占着。
            try:
                self.store.expire_stalled_jobs()
            except Exception:  # pragma: no cover - keep the worker alive
                LOGGER.exception("failed to expire stalled jobs")
            try:
                job = self.store.claim_next_job(lease_seconds=self.job_timeout + 60.0)
            except Exception:  # pragma: no cover - keep the worker alive
                LOGGER.exception("failed to claim a parse job")
                self._stop.wait(1.0)
                continue
            if job is None:
                self._stop.wait(0.5)
                self._retire_sessions()
                continue
            self._run_job(job)
            self._retire_sessions()

    def _run_job(self, job: Mapping[str, Any]) -> None:
        job_id = job["job_id"]
        payload = self.store.job_request(job_id)
        if payload is None:
            self.store.finish_job(job_id, JOB_FAILED, error_code="missing_request", error_message="任务内容已丢失，请在软件中重试")
            return
        if self.store.cancel_requested(job_id):
            self.store.finish_job(job_id, JOB_CANCELLED, error_code="cancelled", error_message="任务已取消")
            return
        try:
            session_id = self.adapter.create_session(job_id)
        except AdapterError as exc:
            self.store.finish_job(job_id, JOB_FAILED, error_code="no_session", error_message=exc.merchant_message)
            return
        # Persist the upstream session id *before* the prompt leaves the process.
        # Without this, a crash mid-request leaves no handle to reconcile with
        # and the only recovery would be a re-run that double-charges the customer.
        self.store.mark_upstream_session(job_id, session_id)
        deadline = time.time() + self._job_budget_seconds(job)
        try:
            outcome = self._run_with_job_model(job_id, payload, deadline, session_id)
        except (AdapterTimeout, OutcomeUnknown) as exc:
            self.store.record_internal_retry(job_id, "上游结果未确认", tokens=0)
            self.store.finish_job(job_id, JOB_OUTCOME_UNKNOWN, error_code="outcome_unknown", error_message=exc.merchant_message)
            # 结果未知：**不**释放再重调。预留留在原处等对账决定，否则
            # 同一笔工作会被当成"没跑过"再执行一次、扣两次。
            LOGGER.warning("job %s outcome unknown: %s", job_id, exc)
            return
        except (InvalidParseOutput, AdapterRejected) as exc:
            self.store.record_internal_retry(job_id, "上游未产出可用结果，系统成本不计入客户")
            self.store.finish_job(job_id, JOB_FAILED, error_code="invalid_output" if isinstance(exc, InvalidParseOutput) else "rejected", error_message=exc.merchant_message)
            self._queue_session_cleanup(session_id)
            return
        except AdapterUnavailable as exc:
            self.store.record_internal_retry(job_id, "后端不可达")
            self.store.finish_job(job_id, JOB_FAILED, error_code="backend_unavailable", error_message=exc.merchant_message)
            self._queue_session_cleanup(session_id)
            return
        except AdapterError as exc:
            self.store.finish_job(job_id, JOB_FAILED, error_code="adapter_error", error_message=exc.merchant_message)
            return
        except Exception:
            LOGGER.exception("parse job %s failed unexpectedly", job_id)
            self.store.finish_job(job_id, JOB_FAILED, error_code="internal_error", error_message="服务暂时不可用，请稍后再试")
            return
        if self.store.cancel_requested(job_id):
            self.adapter.abort(session_id)
            self.store.finish_job(job_id, JOB_CANCELLED, error_code="cancelled", error_message="任务已取消")
            self._queue_session_cleanup(session_id)
            return
        self._queue_session_cleanup(session_id)
        # 计费按**任务冻结的快照**，不按适配器返回值 —— 否则一个固定
        # cost_micro_usd=1 的适配器会给免费任务也记上一笔；反过来，若只信适配器，
        # 运营配的「每条消息扣 3 次」会变成实际只扣 1 次，显示与扣费就对不上。
        snapshot = self.store.job_snapshot(job_id) or {}
        charged = self._frozen_charge_micro_usd(job_id, snapshot, outcome)
        # 请求的模型与**上游自报**实际执行的模型分开记。上游没回标识就是
        # 「未能独立确认」，不是"确认一致"；对不上就明确留一条警告。
        self.store.mark_model_execution(
            job_id,
            requested=getattr(outcome, "model_requested", "") or f"{snapshot.get('provider_id','')}/{snapshot.get('model_id','')}",
            reported=getattr(outcome, "model_reported", ""),
            confirmed=bool(getattr(outcome, "model_confirmed", False)),
            warning=getattr(outcome, "model_warning", ""),
            evidence=getattr(outcome, "request_evidence", {}),
        )
        if getattr(outcome, "model_warning", ""):
            LOGGER.warning("job %s model identity: %s", job_id, outcome.model_warning)
        self.store.finish_job(job_id, JOB_SUCCEEDED, result=outcome.result, charged_micro_usd=charged)

    def _frozen_charge_micro_usd(
        self, job_id: str, snapshot: Mapping[str, Any], outcome: Any
    ) -> int:
        """这个任务实��扣多少（单位：整数微美元）。

        优先级：

        1. **任务冻结的模型单价 + 上游回的真实 token 用量** —— 这是真实成本，
           也是商户最终被扣的钱。计价单位是微美元，所以不存在"扣 0 次"的问题。
        2. **冻结的固定额度** ``cost_micro_usd_per_job`` —— 单价拿不到时的回退。
           那是运营显式配的固定值，比猜一个强。
        3. 都没有 → 适配器自报。

        为什么不再无条件用冻结值：冻结的是**预留上限**，不是实扣额。
        无条件按预留扣，商户就会为"模型最多可能用到的量"付钱 ——
        消息短的时候，那些多扣的钱再也回不来。
        """
        del job_id
        if snapshot.get("pricing_class") == "free":
            return 0
        price = self._frozen_price(snapshot)
        if price is not None:
            settled = settle_micro_usd_for(
                int(getattr(outcome, "input_tokens", 0) or 0),
                int(getattr(outcome, "output_tokens", 0) or 0),
                price,
            )
            if settled is not None:
                return settled
        frozen = (snapshot.get("snapshot") or {}).get("cost_micro_usd_per_job")
        if frozen is not None:
            return max(0, int(frozen))
        # 没有快照的旧任务：退回适配器自报，仍然不猜测。
        return int(getattr(outcome, "cost_micro_usd", 0) or DEFAULT_COST_MICRO_USD)

    @staticmethod
    def _frozen_price(snapshot: Mapping[str, Any]) -> ModelPrice | None:
        """从任务冻结的策略里取出单价。

        冻结的是**价格**不是 token 数：商户确认过的是这个单价，用完这次之后
        运营再改价也不该影响已经在跑的任务。

        输入与输出单价**必须同时有效**才返回价格。只有一个有效时不能凑合 ——
        那样会算出真实成本的一半，按那个数字扣商户的钱就是在少扣。
        """
        frozen = snapshot.get("snapshot") or {}
        price_in = _optional_price(frozen, "input_price_per_million")
        price_out = _optional_price(frozen, "output_price_per_million")
        if price_in is None and price_out is None:
            return None
        return ModelPrice(
            model=str(frozen.get("model_id") or ""),
            input_per_million=price_in or 0.0,
            output_per_million=price_out or 0.0,
        )

    def _job_budget_seconds(self, job: Mapping[str, Any]) -> float:
        """按任务**冻结的** ``max_task_seconds`` 给这次执行定时。

        用网关全局值会忽略策略里更严的模型上限；用适配器自身的读超时则完全不
        受运营配置控制。取两者与任务冻结值的最小值。
        """
        snapshot = self.store.job_snapshot(str(job.get("job_id", ""))) or {}
        frozen = float((snapshot.get("snapshot") or {}).get("max_task_seconds") or 0.0)
        budget = min(self.job_timeout, frozen) if frozen > 0 else self.job_timeout
        return max(5.0, budget)

    def _run_with_job_model(self, job_id: str, payload: Mapping[str, Any], deadline: float, session_id: str = ""):
        """用任务**创建时冻结**的模型执行。

        客户换了模型、目录刷新了、上游下线了，都不影响已经在跑的任务 ——
        这同时也是"网络重试用原模型、原计费快照"的实现方式。

        模型是**参数**传下去的，不是改适配器属性：适配器是所有工作线程共用的
        单例，改属性会让同时在跑的两个任务互相换模型。参数化之后，每次请求
        带的都是它自己那份冻结路由。
        """
        snapshot = self.store.job_snapshot(job_id) or {}
        provider_id = str(snapshot.get("provider_id") or "")
        model_id = str(snapshot.get("model_id") or "")
        return self.adapter.run_parse(
            session_id, payload, job_id, deadline,
            cancel_check=lambda: self.store.cancel_requested(job_id),
            provider_id=provider_id, model_id=model_id,
        )

    def _queue_session_cleanup(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions_to_clean.append(session_id)

    def _retire_sessions(self) -> None:
        """Delete upstream sessions once their job outcome is durably recorded."""
        with self._lock:
            pending = list(self._sessions_to_clean)
            self._sessions_to_clean.clear()
        for session_id in pending:
            try:
                self.adapter.delete_session(session_id)
            except Exception:  # pragma: no cover - best effort cleanup
                LOGGER.exception("failed to delete OpenCode session")

    def reconcile_job(self, job_id: str) -> dict[str, Any]:
        """Operator action: decide the real outcome of an unconfirmed job."""
        row = self.store.job_row(job_id)
        if row is None:
            raise NotFound("任务不存在", code="job_not_found")
        if row["state"] not in {JOB_OUTCOME_UNKNOWN, "awaiting_reconcile"}:
            raise Conflict("该任务无需确认", code="not_reconcilable")
        if int(row["charged_micro_usd"]):
            raise Conflict("该任务已结算", code="already_settled")
        session_id = str(row["upstream_session_id"] or "")
        snapshot = self.store.job_snapshot(job_id) or {}
        found = self.adapter.reconcile(session_id) if session_id else None
        if found is None:
            # 上游会话没了：从来没留下结果。释放预留，**不**重跑。
            self._queue_session_cleanup(session_id)
            return self.store.finish_job(
                job_id, JOB_FAILED, error_code="no_upstream_result", error_message=MERCHANT_OUTCOME_MISSING
            )
        if found.get("text"):
            # 确认它**跑过**：补记一次用量，而不是让它白跑。
            self._queue_session_cleanup(session_id)
            result = validate_parse_result(extract_json_object(found["text"]))
            # 与正常完成走同一个扣费口径，同样只认冻结的策略数字。
            charged = self._frozen_charge_micro_usd(job_id, snapshot, None)
            view = self.store.finish_job(job_id, JOB_SUCCEEDED, result=result, charged_micro_usd=charged)
            return view
        # 还没定：状态保持不变，预留也留着。继续等，不盲目重发。
        return self.store.get_job_any(row["account_id"], job_id)

    # ------------------------------------------------------------- responses

    def _health(self) -> GatewayResponse:
        body: dict[str, Any] = {"ok": True, "service": "pyweixin-gateway", **self.store.health()}
        return _json(200, body)

    def execution_backend_report(self) -> dict[str, Any]:
        report = dict(self.adapter_report)
        if not report:
            try:
                report = self.adapter.probe()
            except Exception as exc:  # pragma: no cover
                report = {"ok": False, "error": str(exc)}
        return report

    def platform_budget_status(self) -> dict[str, Any]:
        """平台预算的当前读数。没有配置时如实说"未设置"，不编造用量。"""
        ledger = self.platform_budget
        if ledger is None:
            return {"configured": False}
        try:
            status = dict(ledger.status())
        except Exception:  # pragma: no cover - 读数失败不应影响商户
            return {"configured": True, "available": False}
        status["configured"] = True
        return status

    def _account_payload(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "merchant": {
                "display_name": identity["display_name"],
                "username": identity["username"],
                "merchant_id": identity["merchant_id"],
            },
            "device": {"id": identity["device_id"], "name": identity["device_name"]},
            "subscription": self.store.get_subscription(identity["account_id"]),
            "must_change_password": identity.get("must_change_password", False),
        }

    def _require_client(self, headers: Mapping[str, str]) -> dict[str, Any]:
        authorization = _header(headers, "Authorization")
        if not authorization.startswith("Bearer "):
            raise Unauthenticated("登录已过期，请重新登录软件", code="unauthenticated")
        identity = self.store.authenticate_access_token(authorization[7:])
        if identity is None:
            raise Unauthenticated("登录已过期，请重新登录软件", code="unauthenticated")
        return identity

    def _require_session(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> dict[str, Any]:
        session = self.store.load_web_session(cookies.get(SESSION_COOKIE, ""))
        if session is None:
            raise Unauthenticated("请先在网页中登录", code="unauthenticated")
        return session

    def _require_operator(self, headers: Mapping[str, str]) -> dict[str, Any]:
        authorization = _header(headers, "X-Operator-Token")
        if not authorization:
            raise Unauthenticated("运营接口需要运营令牌", code="unauthenticated")
        if not self.operator_token or not hmac.compare_digest(authorization, self.operator_token):
            raise Forbidden("运营令牌无效", code="forbidden")
        return {"role": "operator"}

    def _operator_create_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", "")) or _generated_password()
        account = self.store.create_account(
            username,
            password,
            merchant_id=str(payload.get("merchant_id", "")) or "",
            display_name=str(payload.get("display_name", "")),
            plan_code=str(payload.get("plan_code", "trial")),
            quota_micro_usd=int(payload.get("quota_micro_usd", 0)),
            max_concurrent_jobs=int(payload.get("max_concurrent_jobs", 2)),
            period_days=int(payload.get("period_days", 30)),
        )
        return {"account": account, "note": "初始密码仅显示一次，请通过安全渠道交给商家并要求首次登录修改"}

    def _operator_update_account(self, account_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if "quota_micro_usd" in payload:
            updates["quota_micro_usd"] = int(payload["quota_micro_usd"])
        if "plan_code" in payload:
            updates["plan_code"] = str(payload["plan_code"])
        if "max_concurrent_jobs" in payload:
            updates["max_concurrent_jobs"] = int(payload["max_concurrent_jobs"])
        if "period_days" in payload:
            updates["period_days"] = int(payload["period_days"])
        if "extend_period" in payload:
            updates["extend_period"] = bool(payload["extend_period"])
        if "status" in payload:
            self.store.set_account_status(account_id, str(payload["status"]))
        subscription = self.store.update_subscription(account_id, **updates) if updates else self.store.get_subscription(account_id)
        return {"subscription": subscription}

    # ---------------------------------------------------------- cookie utils

    def session_cookie_value(self, session_id: str) -> str:
        return session_id


MERCHANT_OUTCOME_MISSING = "未获取到解析结果，请在软件中重新提交"


def _parse_cookies(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


def _header(headers: Mapping[str, str], name: str, default: str = "") -> str:
    """Case-insensitive header lookup.

    Header names are case-insensitive on the wire, and some clients (urllib
    among them) normalise the case they send, so exact-key lookups silently
    fail and would look like a CSRF or auth failure to the merchant.
    """
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if str(key).lower() == lowered:
            return candidate
    return default


def _client_ip(headers: Mapping[str, str]) -> str:
    return _header(headers, "X-Forwarded-For").split(",")[0].strip() or "local"


def _empty_catalog() -> Any:
    """没有配置目录时的占位。

    刻意返回**空目录**而不是「退回固定模型」：没有目录就没有可选模型，
    客户会看到「免费模型暂不可用」，而不是被悄悄收走额度。
    """
    from .model_catalog import CatalogStore

    return CatalogStore()


def _budget_with_ledger(store: Any, budget: Any) -> Any:
    """把平台预算配置接上真正的账本。

    :class:`~pyweixin.product.PlatformBudget` 只是**配置**（几个上限数字），
    它不知道今天已经跑了多少；``can_accept`` 因此恒为真。真正读账的是
    :class:`~pyweixin.product.PlatformBudgetLedger`。不自动接上的话，一个看起来
    配好了的预算在生产里等于没配。
    """
    from .product import PlatformBudget as _Budget
    from .product import PlatformBudgetLedger

    if budget is None:
        return None
    if isinstance(budget, PlatformBudgetLedger):
        return budget
    if isinstance(budget, _Budget):
        # 用 store 自己的时钟，测试里的假时钟才和账本对得上。
        return PlatformBudgetLedger(store, budget, clock=getattr(store, "_clock", None))
    return budget


def _decode_json(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValidationFailed("请求必须是 JSON 对象", code="invalid_request")
    return payload


def _json(status: int, payload: Mapping[str, Any]) -> GatewayResponse:
    return GatewayResponse(status=status, payload=dict(payload))


def _html(status: int, markup: str) -> GatewayResponse:
    return GatewayResponse(status=status, html=markup, content_type="text/html; charset=utf-8")


def _generated_password(length: int = 16) -> str:
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(12, length)))


__all__ = [
    "ProductGateway",
    "GatewayResponse",
    "DEFAULT_COST_MICRO_USD",
    "OpenCodeAdapter",
    "LocalRuleAdapter",
    "Conflict",
    "Forbidden",
    "IdempotencyConflict",
    "NotFound",
    "QuotaExhausted",
    "Unauthenticated",
    "ValidationFailed",
    "ProductError",
    "build_agent_prompt",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "JOB_SUCCEEDED",
    "JOB_FAILED",
    "JOB_CANCELLED",
    "JOB_OUTCOME_UNKNOWN",
]
