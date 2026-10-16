"""Gateway-facing adapter over the reusable OpenCode backend.

This module is now a **thin adapter**, not a second implementation. The HTTP
transport, session handling, version probing and JSON extraction live in
``business_agent.backends.opencode``, which is reusable and knows nothing about
this product.

Why the split exists: the gateway's job queue speaks in terms of *jobs* that
already have a session id (it must persist the upstream session id before the
prompt leaves the process, or a crash loses the only reconciliation handle).
The core's backend speaks in terms of *runs*. This module translates between
them and re-exports the error types the gateway already catches.

It also removes a reversed dependency: this module used to import the order
schema from ``wechat_ai_assistant.orders``, which made the "generic backend" depend on the
order domain. Order validation now lives in ``order_agent`` and is reached
through the core's plugin layer.

Verified OpenCode facts (see docs/opencode-verification.md) are documented in
``business_agent/backends/opencode.py`` where they are now enforced.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from ._packages import ensure_packages_on_path

ensure_packages_on_path()

from business_agent.backends.opencode import (  # noqa: E402
    PINNED_OPENCODE_VERSION,
    REQUIRED_DENIED_PERMISSIONS,
    REQUIRED_ENDPOINTS,
    OpenCodeBackend,
    _version_matches,  # re-exported: pinned-version checks are part of this API
    render_prompt,
)
from business_agent.contracts.errors import AgentError, ErrorCategory  # noqa: E402
from business_agent.contracts.protocols import BackendResult, PromptPackage  # noqa: E402
from business_agent.contracts.request import AgentRequest, AsOf, Budget, Event, Policy, Scope  # noqa: E402
from business_agent.core import validation as _core_validation  # noqa: E402
from business_agent.contracts.request import AgentRequest, AsOf, Budget, Event, Policy, Scope  # noqa: E402
from order_agent.contract import ALLOWED_INTENTS, validate_output  # noqa: E402


def extract_json_object(text: str) -> dict[str, Any]:
    """Product-facing wrapper that raises this module's error type.

    The extraction rule itself is the core's and is unchanged; only the
    exception type is translated, so the gateway's existing ``except
    InvalidParseOutput`` clauses keep working.
    """
    try:
        return _core_validation.extract_json_object(text)
    except Exception as exc:
        raise InvalidParseOutput(MERCHANT_MESSAGES["invalid_output"]) from exc

# Re-exported so the rest of the product keeps importing these names from here.
__all__ = [
    "AdapterError",
    "AdapterRegistry",
    "AdapterRejected",
    "AdapterTimeout",
    "AdapterUnavailable",
    "ALLOWED_INTENTS",
    "CLOUD_ORDER_INTENTS",
    "InvalidParseOutput",
    "LocalRuleAdapter",
    "MERCHANT_MESSAGES",
    "ORDER_PARSE_AGENT",
    "OrderParseAdapter",
    "OutcomeUnknown",
    "PINNED_OPENCODE_VERSION",
    "ParseOutcome",
    "REQUIRED_DENIED_PERMISSIONS",
    "REQUIRED_ENDPOINTS",
    "OpenCodeAdapter",
    "ScriptedBackend",
    "_version_matches",
    "build_agent_prompt",
    "extract_json_object",
    "validate_parse_result",
]

ORDER_PARSE_AGENT = "order-parse"

#: Intents this product is willing to accept from a backend.
CLOUD_ORDER_INTENTS = ALLOWED_INTENTS

#: Merchant-facing copy. Deliberately free of provider, token or session detail.
MERCHANT_MESSAGES = {
    "unavailable": "解析服务暂时不可用，请稍后重试",
    "timeout": "解析超时，本次结果未知，可在软件中重试",
    "invalid_output": "解析结果格式异常，请重试或转人工处理",
    "no_agent": "解析服务配置缺失，请联系客服",
    "version_mismatch": "解析服务版本不符，请联系客服",
}


# --------------------------------------------------------------- error types
# Kept as the product's own names so the gateway's existing `except` clauses
# keep working. Each one maps onto a core error category.


class AdapterError(RuntimeError):
    """Base adapter failure. ``retryable`` tells the queue whether to re-queue."""

    retryable = False
    merchant_message = MERCHANT_MESSAGES["unavailable"]


class AdapterUnavailable(AdapterError):
    """The backend was not reached; the parse certainly did not run."""

    retryable = True
    merchant_message = MERCHANT_MESSAGES["unavailable"]


class AdapterRejected(AdapterError):
    """The backend received and refused the request; nothing was executed."""

    retryable = False
    merchant_message = MERCHANT_MESSAGES["unavailable"]


class AdapterTimeout(AdapterError):
    """The prompt was submitted but no result arrived."""

    retryable = False
    merchant_message = MERCHANT_MESSAGES["timeout"]


class OutcomeUnknown(AdapterError):
    """Execution status is undetermined. Never blind-retry: reconcile first."""

    retryable = False
    merchant_message = MERCHANT_MESSAGES["timeout"]


class InvalidParseOutput(AdapterError):
    """The agent answered but the payload failed structured validation."""

    retryable = False
    merchant_message = MERCHANT_MESSAGES["invalid_output"]


_CORE_TO_ADAPTER = {
    ErrorCategory.NETWORK: AdapterUnavailable,
    ErrorCategory.INPUT: AdapterRejected,
    ErrorCategory.CONFIG: AdapterRejected,
    ErrorCategory.RATE_LIMITED: AdapterUnavailable,
    ErrorCategory.TIMEOUT: AdapterTimeout,
    ErrorCategory.OUTCOME_UNKNOWN: OutcomeUnknown,
    ErrorCategory.OUTPUT_INVALID: InvalidParseOutput,
}


def _translate(exc: AgentError) -> AdapterError:
    """Convert a core error into the product's own adapter error."""
    cls = _CORE_TO_ADAPTER.get(exc.category, AdapterError)
    translated = cls(str(exc.detail) or exc.category)
    translated.merchant_message = exc.merchant_message or MERCHANT_MESSAGES["unavailable"]
    backend_handle = getattr(exc, "backend_handle", "")
    if backend_handle:
        translated.backend_handle = backend_handle
    return translated


class _Translator:
    """Call into the core backend and re-raise its errors as adapter errors."""

    def __init__(self, backend: OpenCodeBackend):
        self.backend = backend

    def __getattr__(self, name: str) -> Callable[..., Any]:
        attribute = getattr(self.backend, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attribute(*args, **kwargs)
            except AgentError as exc:
                raise _translate(exc) from exc

        return call


# ------------------------------------------------------------------ contract


def validate_parse_result(result: Any) -> dict[str, Any]:
    """Merchant-facing wrapper around the order plugin's output validator.

    The structure check itself lives next to the schema it enforces, so the
    gateway and the merchant's local business layer can never accept different
    shapes. This wrapper only translates the error type.
    """
    try:
        return validate_output(result)
    except Exception as exc:  # order_agent raises OutputInvalid
        raise InvalidParseOutput(MERCHANT_MESSAGES["invalid_output"]) from exc


from dataclasses import dataclass  # noqa: E402,F811  (kept for the re-export below)


@dataclass(frozen=True)
class ParseOutcome:
    result: dict[str, Any]
    session_id: str = ""
    message_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 1
    #: 本次**请求**的 provider/model（来自任务冻结的快照）。
    model_requested: str = ""
    #: 上游**自报**实际执行的 provider/model。上游没回就是空串。
    model_reported: str = ""
    #: 上游自报与请求是否一致。**上游没回标识时是 False** ——
    #: "没说出来"不等于"确认一致"。
    model_confirmed: bool = False
    #: 核对不通过或无法核对时的说明。空串 = 一致或已确认。
    model_warning: str = ""
    #: 脱敏后的请求结构证据：**不得**包含认证头或密钥。
    request_evidence: dict[str, Any] = field(default_factory=dict)


class OrderParseAdapter(Protocol):
    """What the gateway needs from any execution backend.

    Session creation is deliberately separate from prompt submission: the
    gateway must persist the session id *before* sending the prompt, otherwise a
    crash mid-request loses the only handle needed to reconcile the outcome.
    """

    def health(self) -> dict[str, Any]: ...

    def probe(self) -> dict[str, Any]: ...

    def create_session(self, job_id: str) -> str: ...

    def run_parse(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        job_id: str,
        deadline: float,
        cancel_check: Callable[[], bool] | None = None,
        provider_id: str = "",
        model_id: str = "",
    ) -> ParseOutcome: ...

    def abort(self, session_id: str) -> bool: ...

    def delete_session(self, session_id: str) -> bool: ...

    def reconcile(self, session_id: str) -> dict[str, Any] | None: ...


class OpenCodeAdapter:
    """Native OpenCode session-API adapter, delegating to the reusable backend.

    The public surface is unchanged, so the gateway worker, its tests and its
    reconciliation path are untouched. Everything below the surface -- HTTP,
    sessions, probing, error classification -- is the core package's.
    """

    def __init__(
        self,
        base_url: str,
        username: str = "opencode",
        password: str = "",
        agent: str = ORDER_PARSE_AGENT,
        provider_id: str = "",
        model_id: str = "",
        connect_timeout: float = 5.0,
        read_timeout: float = 300.0,
        expected_version: str = PINNED_OPENCODE_VERSION,
        prompt_overrides: Mapping[str, Any] | None = None,
    ):
        try:
            self._backend = OpenCodeBackend(
                base_url=base_url,
                password=password,
                username=username,
                agent=agent,
                provider_id=provider_id,
                model_id=model_id,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                expected_version=expected_version,
            )
        except AgentError as exc:
            # The product's callers expect ValueError for a misconfigured
            # adapter; keep that contract rather than leaking the core's type.
            raise ValueError(str(exc.detail)) from exc
        self._call = _Translator(self._backend)
        self.base_url = self._backend.base_url
        self.agent = agent
        self.prompt_overrides = dict(prompt_overrides or {})

    # ------------------------------------------------------------- lifecycle

    def health(self) -> dict[str, Any]:
        return self._call.health()

    def list_providers(self) -> list[dict[str, Any]]:
        """供应商与模型目录（凭据已在核心后端剔除）。

        供网关构建客户模型目录。"目录里有"和"已连接"是两回事，两者都在
        返回值里，由调用方按自己的准入规则决定。
        """
        return self._backend.list_providers()

    def list_connected_models(self) -> list[dict[str, Any]]:
        """只有**已连接**的供应商。"""
        return self._backend.list_connected_models()

    def probe(self) -> dict[str, Any]:
        """Verify the running server really exposes the API this adapter needs.

        Writing an agent config file proves nothing; this checks the live
        server so a silently-unloaded permission config is reported instead of
        assumed effective.
        """
        return self._call.probe(self.agent)

    def create_session(self, job_id: str) -> str:
        # The title is an opaque job id only: no merchant data reaches OpenCode
        # metadata, and no other merchant's context is reused.
        return self._call.create_session(job_id)

    def abort(self, session_id: str) -> bool:
        return bool(self._call.abort(session_id))

    def delete_session(self, session_id: str) -> bool:
        return bool(self._call.delete_session(session_id))

    def reconcile(self, session_id: str) -> dict[str, Any] | None:
        """Re-read a session after a crash/timeout to decide the real outcome."""
        return self._call.reconcile(session_id)

    # ------------------------------------------------------------- execution

    def run_parse(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        job_id: str,
        deadline: float,
        cancel_check: Callable[[], bool] | None = None,
        provider_id: str = "",
        model_id: str = "",
    ) -> ParseOutcome:
        """Run one parse in an already-created session.

        The gateway owns the session lifecycle (it persists the id before the
        prompt leaves), so this reuses ``session_id`` instead of letting the
        backend create a new one.

        ``provider_id`` / ``model_id`` are the **job's frozen** routing and are
        passed straight through to the request body. They are arguments, not
        instance attributes: several jobs can be in flight at once, and a shared
        attribute would let one job's model be sent with another job's prompt.
        """
        if not session_id:
            raise OutcomeUnknown("missing upstream session id")
        if cancel_check and cancel_check():
            raise AdapterRejected("cancelled before prompt")
        if self.prompt_overrides:
            payload = {**dict(payload), **self.prompt_overrides}
        request = _stub_request(payload, job_id)
        prompt = PromptPackage(
            system="", payload=dict(payload), run_label=job_id
        )
        run_provider = provider_id or self._backend.provider_id
        run_model = model_id or self._backend.model_id
        # 脱敏后的请求结构证据：只有结构和路由，没有认证头、没有密钥。
        evidence = {
            "method": "POST",
            "path_template": "/session/{sessionID}/message",
            "agent": self._backend.agent,
            "model": {"providerID": run_provider, "modelID": run_model} if run_provider and run_model else {},
            "payload_keys": sorted(str(k) for k in payload) if isinstance(payload, Mapping) else [],
        }
        try:
            outcome = self._run_in_session(
                session_id, prompt, request, provider_id=run_provider, model_id=run_model, deadline=deadline
            )
        except AdapterError:
            raise
        reported = str(outcome.model_reported or "")
        requested = f"{run_provider}/{run_model}" if run_provider and run_model else ""
        confirmed = bool(reported) and reported == requested
        if not reported:
            warning = "上游未返回模型标识，未能独立确认实际执行的模型"
        elif not confirmed:
            warning = f"上游实际执行的模型与请求不一致：请求 {requested or '(未指定)'}，实际 {reported}"
        else:
            warning = ""
        return ParseOutcome(
            result=validate_parse_result(outcome.raw or _parse_json(outcome.text)),
            session_id=session_id,
            message_id=outcome.handle,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            model_requested=requested,
            model_reported=reported,
            model_confirmed=confirmed,
            model_warning=warning,
            request_evidence=evidence,
        )

    def _run_in_session(
        self,
        session_id: str,
        prompt: PromptPackage,
        request: AgentRequest,
        *,
        provider_id: str = "",
        model_id: str = "",
        deadline: float = 0.0,
    ) -> BackendResult:
        """Drive the core backend against an existing session."""
        original_create = self._backend.create_session
        try:
            self._backend.create_session = lambda _label: session_id  # type: ignore[method-assign]
            return self._call.run(
                prompt, request, provider_id=provider_id, model_id=model_id,
                read_timeout=_remaining_budget(deadline),
            )
        finally:
            self._backend.create_session = original_create  # type: ignore[method-assign]

    # -------------------------------------------------------------- accessors

    @property
    def model(self) -> str:
        return self._backend.model

    @property
    def provider_id(self) -> str:
        return self._backend.provider_id

    @provider_id.setter
    def provider_id(self, value: str) -> None:
        self._backend.provider_id = str(value or "")

    @property
    def model_id(self) -> str:
        return self._backend.model_id

    @model_id.setter
    def model_id(self, value: str) -> None:
        self._backend.model_id = str(value or "")


def _remaining_budget(deadline: float) -> float:
    """把任务的冻结时限变成这次 HTTP 调用的读超时。

    0 表示"不限"，交回后端自己的 ``read_timeout``。给一个下限，避免截止时间
    已经过去时抛出一个 0 秒超时。
    """
    try:
        remaining = float(deadline) - time.time()
    except (TypeError, ValueError):
        return 0.0
    if remaining <= 0:
        return 0.0
    return max(5.0, remaining)


def _parse_json(text: str) -> dict[str, Any]:
    try:
        return extract_json_object(text)
    except Exception as exc:
        raise InvalidParseOutput(MERCHANT_MESSAGES["invalid_output"]) from exc


def _stub_request(payload: Mapping[str, Any], job_id: str) -> AgentRequest:
    """A minimal request for the backend's transport-level concerns.

    The real domain request is built by the plugin layer; the backend only needs
    a budget and a request id, so this is a placeholder rather than a second
    way to construct a business request.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return AgentRequest(
        request_id=job_id,
        idempotency_key=job_id,
        task_type="order_parse",
        scope=Scope(tenant_id="gateway"),
        events=(Event(event_id=job_id, body="parse", occurred_at=now),),
        as_of=AsOf(instant=now),
        policy=Policy(name="gateway"),
        budget=Budget(timeout_seconds=120.0),
    )


def build_agent_prompt(payload: Mapping[str, Any]) -> str:
    """Render the untrusted parse request as inert JSON data.

    The group message is placed inside a JSON document and the wrapper states
    explicitly that it is data, not instructions. The server-side system prompt
    in the agent definition is the only instruction channel.
    """
    return render_prompt(PromptPackage(system="", payload=dict(payload)))


class LocalRuleAdapter:
    """Deterministic offline stand-in used by tests and by a no-model install.

    It is **not** a model. It exists so the queue, quota, cancellation and
    recovery paths can be exercised end to end without an upstream provider, and
    it is always reported as ``is_model = False`` so it can never be mistaken
    for a verified OpenCode run.
    """

    is_model = False

    def __init__(self, response: Mapping[str, Any] | None = None, fail_with: str = ""):
        self._response = dict(response) if response else None
        self._fail_with = fail_with
        #: Append-only record of every session ever created, so tests can assert
        #: on creation even after the session has been deleted again.
        self.created_sessions: list[str] = []
        self.sessions: list[str] = []
        #: The routing each call actually asked for. Not a model, but it is the
        #: place a test can prove the *selected* model reached the backend.
        self.calls: list[dict[str, str]] = []

    def health(self) -> dict[str, Any]:
        return {"healthy": not self._fail_with, "version": "local-rules"}

    def probe(self) -> dict[str, Any]:
        return {
            "base_url": "local",
            "ok": not self._fail_with,
            "is_model": False,
            "checks": {"health": self.health(), "agent": {"available": True, "name": "local-rules"}},
            "error": self._fail_with,
        }

    def create_session(self, job_id: str) -> str:
        session_id = "local_" + job_id.replace("job_", "")
        self.sessions.append(session_id)
        self.created_sessions.append(session_id)
        return session_id

    def run_parse(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        job_id: str,
        deadline: float,
        cancel_check: Callable[[], bool] | None = None,
        provider_id: str = "",
        model_id: str = "",
    ) -> ParseOutcome:
        del deadline
        self.calls.append(
            {"job_id": str(job_id), "provider_id": str(provider_id or ""), "model_id": str(model_id or "")}
        )
        if self._fail_with == "timeout":
            raise AdapterTimeout("simulated timeout")
        if self._fail_with == "unavailable":
            raise AdapterUnavailable("simulated outage")
        if self._fail_with == "invalid":
            raise InvalidParseOutput("simulated invalid output")
        if self._fail_with == "unknown":
            raise OutcomeUnknown("simulated lost response")
        if cancel_check and cancel_check():
            raise AdapterRejected("cancelled")
        if self._response is not None:
            result = validate_parse_result(self._response)
        else:
            result = validate_parse_result(
                {
                    "intent": "unclear",
                    "customer_alias": None,
                    "target_order": None,
                    "items": [],
                    "delivery_date": None,
                    "evidence": [],
                    "missing_fields": ["local-rule-adapter-has-no-model"],
                    "ambiguities": ["本次结果来自本地规则适配器，未经过模型验证"],
                    "suggested_change": None,
                }
            )
        return ParseOutcome(result=result, session_id=session_id, message_id="m_" + session_id)

    def abort(self, session_id: str) -> bool:
        return session_id in self.sessions

    def delete_session(self, session_id: str) -> bool:
        if session_id in self.sessions:
            self.sessions.remove(session_id)
            return True
        return False

    def reconcile(self, session_id: str) -> dict[str, Any] | None:
        return None


class ScriptedBackend:
    """按消息文本返回**预先写好的**结构化结果。

    **它不是模型，也不会假装自己是。** 存在的唯一目的是把"业务应用路径"
    单独拿出来验：客户、SKU、单位、数量、意图、证据这些字段从云端结果到
    订单、版本、备货的落地是否正确。

    拿它验出来的结论只能说"业务落地正确"，**不能说**"模型理解正确"。
    :attr:`is_model` 恒为 False，能力验证与任何准入都会拒绝它。

    用法::

        backend = ScriptedBackend({"来两箱苹果": {...}})
        backend.sessions / backend.calls 可用于断言"确实被调用过"。
    """

    is_model = False
    name = "scripted"

    def __init__(self, responses: Mapping[str, Any] | None = None, default: Any = None, fail_with: str = ""):
        self._responses = dict(responses or {})
        self._default = default
        self._fail_with = fail_with
        self.created_sessions: list[str] = []
        self.sessions: list[str] = []
        #: 每次调用的记录：谁被调了、拿到哪个 key、带什么路由。
        self.calls: list[dict[str, Any]] = []

    def _key_for(self, payload: Mapping[str, Any]) -> str:
        message = payload.get("message") if isinstance(payload, Mapping) else None
        return str(message.get("content", "")) if isinstance(message, Mapping) else ""

    def health(self) -> dict[str, Any]:
        return {"healthy": not self._fail_with, "version": "scripted"}

    def probe(self) -> dict[str, Any]:
        return {
            "base_url": "scripted", "ok": not self._fail_with, "is_model": False,
            "checks": {"health": self.health(), "agent": {"available": True, "name": "scripted"}},
            "error": self._fail_with,
        }

    def create_session(self, job_id: str) -> str:
        session_id = "scripted_" + job_id.replace("job_", "")
        self.sessions.append(session_id)
        self.created_sessions.append(session_id)
        return session_id

    def run_parse(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        job_id: str,
        deadline: float,
        cancel_check: Callable[[], bool] | None = None,
        provider_id: str = "",
        model_id: str = "",
    ) -> ParseOutcome:
        del deadline
        key = self._key_for(payload)
        self.calls.append({
            "job_id": str(job_id), "key": key,
            "provider_id": str(provider_id or ""), "model_id": str(model_id or ""),
        })
        if self._fail_with == "timeout":
            raise AdapterTimeout("scripted timeout")
        if self._fail_with == "unknown":
            raise OutcomeUnknown("scripted lost response")
        if self._fail_with == "invalid":
            raise InvalidParseOutput("scripted invalid output")
        if self._fail_with == "unavailable":
            raise AdapterUnavailable("scripted outage")
        if cancel_check and cancel_check():
            raise AdapterRejected("cancelled")
        suggestion = self._responses.get(key, self._default)
        if suggestion is None:
            raise InvalidParseOutput(f"scripted backend has no scripted answer for {key!r}")
        return ParseOutcome(
            result=validate_parse_result(suggestion),
            session_id=session_id,
            message_id="m_" + session_id,
            model_requested=f"{provider_id}/{model_id}" if provider_id and model_id else "",
            # 编排后端没有上游可言：如实说"未能独立确认"，不编造一致性。
            model_reported="",
            model_confirmed=False,
            model_warning="编排后端没有上游模型，未能独立确认实际执行的模型",
            request_evidence={"backend": "scripted", "key": key},
        )

    def abort(self, session_id: str) -> bool:
        return session_id in self.sessions

    def delete_session(self, session_id: str) -> bool:
        if session_id in self.sessions:
            self.sessions.remove(session_id)
            return True
        return False

    def reconcile(self, session_id: str) -> dict[str, Any] | None:
        del session_id
        return None


from dataclasses import dataclass, field  # noqa: E402,F811  (re-export kept for callers)


@dataclass
class AdapterRegistry:
    """Single source of truth for which backend the worker may use."""

    adapter: Any = field(default_factory=LocalRuleAdapter)

    def probe(self) -> dict[str, Any]:
        try:
            report = self.adapter.probe()
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": str(exc), "checks": {}}
        report.setdefault("is_model", getattr(self.adapter, "is_model", True))
        return report
