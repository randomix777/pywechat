"""Versioned, serialisable result contract.

A result is a *proposal*, not an action.  The core never writes an order, never
sends a message and never mutates a customer's long-term facts: it returns
suggestions plus the evidence behind them, and the host decides what to do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .errors import ErrorCategory
from .request import Scope, utc_now

#: Bumped only for breaking changes to the wire shape.
RESULT_VERSION = "1.0"

#: Merchant-facing wording. Deliberately free of provider, token and session detail.
MERCHANT_MESSAGES = {
    ErrorCategory.CONFIG: "服务配置不完整，请联系客服",
    ErrorCategory.INPUT: "提交的内容无法处理，请检查后重试",
    ErrorCategory.NETWORK: "服务暂时不可用，请稍后重试",
    ErrorCategory.RATE_LIMITED: "请求太频繁，请稍后重试",
    ErrorCategory.TIMEOUT: "处理超时，本次结果未知，可在软件中重试",
    ErrorCategory.CANCELLED: "已取消",
    ErrorCategory.OUTCOME_UNKNOWN: "结果待确认，请在软件中重试",
    ErrorCategory.OUTPUT_INVALID: "处理结果格式异常，请重试或转人工处理",
    ErrorCategory.BUSINESS_AMBIGUITY: "需要人工确认",
}


@dataclass(frozen=True)
class Evidence:
    """A claim, and the literal text that supports it."""

    ref: str
    quote: str
    source: str = ""
    #: True when the quote was verified to appear verbatim in the input.
    verified: bool = False

    def __post_init__(self) -> None:
        if not str(self.ref).strip():
            raise ValueError("evidence requires a ref")
        if not str(self.quote).strip():
            raise ValueError("evidence requires a quote")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "quote": self.quote,
            "source": self.source,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        return cls(
            ref=str(data.get("ref", "")),
            quote=str(data.get("quote", "")),
            source=str(data.get("source", "")),
            verified=bool(data.get("verified", False)),
        )


@dataclass(frozen=True)
class Ambiguity:
    """Something the run could not resolve.

    ``blocking=True`` means the plugin cannot produce a usable proposal without
    a human; ``False`` means the run proceeded and this is a caveat.
    """

    code: str
    detail: str = ""
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "blocking": self.blocking}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Ambiguity":
        return cls(
            code=str(data.get("code", "")),
            detail=str(data.get("detail", "")),
            blocking=bool(data.get("blocking", True)),
        )


@dataclass(frozen=True)
class SuggestedAction:
    """What the host *may* do. The host validates and decides."""

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    #: Why the plugin proposed it -- shown to reviewers, not to merchants.
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": dict(self.payload), "rationale": self.rationale}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SuggestedAction":
        return cls(
            kind=str(data.get("kind", "")),
            payload=data.get("payload") or {},
            rationale=str(data.get("rationale", "")),
        )


@dataclass(frozen=True)
class MemoryProposal:
    """A candidate long-term fact. Long-term memory is the host's to write.

    ``confirmation`` keeps model inference, automatic capture and explicit human
    confirmation strictly separate, so a guess that gets repeated is never
    promoted into a trusted fact.
    """

    #: How the fact was established. Never conflate these.
    MODEL_INFERRED = "model_inferred"
    AUTO_CAPTURED = "auto_captured"
    CUSTOMER_CONFIRMED = "customer_confirmed"
    HOST_CONFIRMED = "host_confirmed"

    key: str
    value: Any
    scope: Scope
    confirmation: str
    source_refs: tuple[str, ...] = ()
    effective_from: datetime | None = None
    #: Identity of the fact this one replaces, for supersession.
    supersedes: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ValueError("memory proposal requires a key")
        if self.confirmation not in (
            self.MODEL_INFERRED,
            self.AUTO_CAPTURED,
            self.CUSTOMER_CONFIRMED,
            self.HOST_CONFIRMED,
        ):
            raise ValueError(f"unsupported memory confirmation: {self.confirmation}")
        object.__setattr__(self, "source_refs", tuple(self.source_refs or ()))

    @property
    def is_trusted(self) -> bool:
        """Only human-confirmed facts may be used as binding business rules."""
        return self.confirmation in (self.CUSTOMER_CONFIRMED, self.HOST_CONFIRMED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "scope": self.scope.to_dict(),
            "confirmation": self.confirmation,
            "source_refs": list(self.source_refs),
            "effective_from": None if self.effective_from is None else self.effective_from.isoformat(),
            "supersedes": self.supersedes,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MemoryProposal":
        raw = data.get("effective_from")
        effective = None if not raw else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return cls(
            key=str(data.get("key", "")),
            value=data.get("value"),
            scope=Scope.from_dict(data.get("scope") or {}),
            confirmation=str(data.get("confirmation", "")),
            source_refs=tuple(data.get("source_refs") or ()),
            effective_from=effective,
            supersedes=str(data.get("supersedes", "")),
            rationale=str(data.get("rationale", "")),
        )


@dataclass(frozen=True)
class Usage:
    """What the run actually consumed. The host decides what to charge."""

    input_tokens: int = 0
    output_tokens: int = 0
    steps: int = 0
    cost_units: int = 0
    #: True when the backend was a real model, not a deterministic stub.
    model_executed: bool = False
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "steps": self.steps,
            "cost_units": self.cost_units,
            "model_executed": self.model_executed,
            "elapsed_seconds": self.elapsed_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Usage":
        return cls(
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            steps=int(data.get("steps", 0)),
            cost_units=int(data.get("cost_units", 0)),
            model_executed=bool(data.get("model_executed", False)),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
        )


@dataclass(frozen=True)
class Versions:
    """Everything needed to reproduce or explain a result later."""

    backend: str = ""
    model: str = ""
    prompt_version: str = ""
    policy_version: str = ""
    plugin_version: str = ""
    contract_version: str = RESULT_VERSION

    def to_dict(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "policy_version": self.policy_version,
            "plugin_version": self.plugin_version,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Versions":
        return cls(
            backend=str(data.get("backend", "")),
            model=str(data.get("model", "")),
            prompt_version=str(data.get("prompt_version", "")),
            policy_version=str(data.get("policy_version", "")),
            plugin_version=str(data.get("plugin_version", "")),
            contract_version=str(data.get("contract_version", RESULT_VERSION)),
        )


class RunStatus:
    """Terminal states of one agent run."""

    SUCCEEDED = "succeeded"
    #: Ran fine, but a human must look before anything is applied.
    NEEDS_REVIEW = "needs_review"
    #: Not enough context; the host may supply more, a bounded number of times.
    NEEDS_CONTEXT = "needs_context"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: The prompt was submitted; nobody knows what the backend did.
    OUTCOME_UNKNOWN = "outcome_unknown"

    ALL = (SUCCEEDED, NEEDS_REVIEW, NEEDS_CONTEXT, FAILED, CANCELLED, OUTCOME_UNKNOWN)


@dataclass(frozen=True)
class AgentResult:
    """The complete, serialisable outcome of one run."""

    run_id: str
    request_id: str
    status: str
    output: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()
    suggested_actions: tuple[SuggestedAction, ...] = ()
    memory_proposals: tuple[MemoryProposal, ...] = ()
    missing_information: tuple[str, ...] = ()
    usage: Usage = field(default_factory=Usage)
    versions: Versions = field(default_factory=Versions)
    error_category: str = ""
    error_detail: str = ""
    merchant_message: str = ""
    #: Opaque handle the host needs to reconcile an unsettled run.
    backend_handle: str = ""
    created_at: datetime = field(default_factory=utc_now)
    result_version: str = RESULT_VERSION

    def __post_init__(self) -> None:
        if self.status not in RunStatus.ALL:
            raise ValueError(f"unknown run status: {self.status}")
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))
        object.__setattr__(self, "ambiguities", tuple(self.ambiguities or ()))
        object.__setattr__(self, "suggested_actions", tuple(self.suggested_actions or ()))
        object.__setattr__(self, "memory_proposals", tuple(self.memory_proposals or ()))
        object.__setattr__(self, "missing_information", tuple(self.missing_information or ()))

    @property
    def ok(self) -> bool:
        return self.status in (RunStatus.SUCCEEDED, RunStatus.NEEDS_REVIEW)

    @property
    def settled(self) -> bool:
        """False when the backend's state is genuinely unknown."""
        return self.status != RunStatus.OUTCOME_UNKNOWN

    @property
    def blocking_ambiguities(self) -> tuple[Ambiguity, ...]:
        return tuple(item for item in self.ambiguities if item.blocking)

    def merchant_summary(self) -> str:
        """Short Chinese line for a merchant UI. Never leaks internals."""
        if self.status == RunStatus.SUCCEEDED:
            return "处理完成"
        if self.status == RunStatus.NEEDS_REVIEW:
            return "需要您确认"
        if self.status == RunStatus.NEEDS_CONTEXT:
            return "缺少必要信息"
        return self.merchant_message or MERCHANT_MESSAGES.get(self.error_category, "处理失败")

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_version": self.result_version,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "status": self.status,
            "output": dict(self.output),
            "evidence": [item.to_dict() for item in self.evidence],
            "ambiguities": [item.to_dict() for item in self.ambiguities],
            "suggested_actions": [item.to_dict() for item in self.suggested_actions],
            "memory_proposals": [item.to_dict() for item in self.memory_proposals],
            "missing_information": list(self.missing_information),
            "usage": self.usage.to_dict(),
            "versions": self.versions.to_dict(),
            "error_category": self.error_category,
            "error_detail": self.error_detail,
            "merchant_message": self.merchant_message,
            "backend_handle": self.backend_handle,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentResult":
        raw_time = data.get("created_at")
        return cls(
            run_id=str(data.get("run_id", "")),
            request_id=str(data.get("request_id", "")),
            status=str(data.get("status", RunStatus.FAILED)),
            output=data.get("output") or {},
            evidence=tuple(Evidence.from_dict(item) for item in data.get("evidence", ())),
            ambiguities=tuple(Ambiguity.from_dict(item) for item in data.get("ambiguities", ())),
            suggested_actions=tuple(SuggestedAction.from_dict(item) for item in data.get("suggested_actions", ())),
            memory_proposals=tuple(MemoryProposal.from_dict(item) for item in data.get("memory_proposals", ())),
            missing_information=tuple(data.get("missing_information") or ()),
            usage=Usage.from_dict(data.get("usage") or {}),
            versions=Versions.from_dict(data.get("versions") or {}),
            error_category=str(data.get("error_category", "")),
            error_detail=str(data.get("error_detail", "")),
            merchant_message=str(data.get("merchant_message", "")),
            backend_handle=str(data.get("backend_handle", "")),
            created_at=(
                datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")) if raw_time else utc_now()
            ),
            result_version=str(data.get("result_version", RESULT_VERSION)),
        )


def evidence_quotes(evidence: Sequence[Evidence]) -> list[str]:
    """Convenience accessor used by plugins and tests."""
    return [item.quote for item in evidence]
