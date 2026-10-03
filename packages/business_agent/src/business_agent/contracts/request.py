"""Versioned, serialisable request contract.

Everything a business agent run needs is expressed here.  The contract is
deliberately domain-neutral: it carries *events*, *context*, *policy* and
*budget*, never ``sku`` or ``tenant_id`` written by a model.

Two properties matter for reuse:

* **Serializable** -- a request can be queued, retried and replayed verbatim.
* **Host-bound scope** -- :class:`Scope` is filled in by the trusted host, not by
  anything a language model produced.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .errors import InputError

#: Bumped only for breaking changes to the wire shape.
CONTRACT_VERSION = "1.0"

#: Upper bounds that keep a hostile or buggy caller from exhausting the process.
MAX_EVENTS = 50
MAX_EVENT_CHARS = 8000
MAX_CONTEXT_ENTRIES = 200
MAX_CONTEXT_CHARS = 200_000
MAX_POLICY_KEYS = 64


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Scope:
    """Business scope, bound by the trusted host.

    A model never gets to choose these values.  Tools bound to a scope refuse
    arguments that try to widen it.
    """

    tenant_id: str
    account_id: str = "default"
    user_id: str = ""
    conversation_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.tenant_id).strip():
            raise InputError("scope.tenant_id is required", "缺少业务归属")

    def to_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "account_id": self.account_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Scope":
        return cls(
            tenant_id=str(data.get("tenant_id", "")),
            account_id=str(data.get("account_id", "default")),
            user_id=str(data.get("user_id", "")),
            conversation_id=str(data.get("conversation_id", "")),
        )


@dataclass(frozen=True)
class Event:
    """One thing that happened: a chat line, a ticket update, a form post."""

    event_id: str
    body: str
    occurred_at: datetime
    actor: str = ""
    channel: str = ""
    kind: str = "text"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.event_id).strip():
            raise InputError("event.event_id is required", "事件缺少标识")
        if not str(self.body).strip():
            raise InputError("event.body must not be empty", "事件内容为空")
        if len(self.body) > MAX_EVENT_CHARS:
            raise InputError(f"event.body exceeds {MAX_EVENT_CHARS} characters", "事件内容过长")
        object.__setattr__(self, "occurred_at", _as_aware(self.occurred_at))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "body": self.body,
            "occurred_at": self.occurred_at.isoformat(),
            "actor": self.actor,
            "channel": self.channel,
            "kind": self.kind,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        occurred = data.get("occurred_at")
        if isinstance(occurred, datetime):
            moment = occurred
        else:
            try:
                moment = datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise InputError("event.occurred_at must be an ISO timestamp", "事件时间无法识别") from exc
        return cls(
            event_id=str(data.get("event_id", "")),
            body=str(data.get("body", "")),
            occurred_at=moment,
            actor=str(data.get("actor", "")),
            channel=str(data.get("channel", "")),
            kind=str(data.get("kind", "text")),
            metadata=data.get("metadata") or {},
        )


@dataclass(frozen=True)
class AsOf:
    """The knowledge cut-off for this run.

    Memory retrieval and history queries must never return anything that only
    became true after this instant.  This is what stops a replay from "seeing"
    the answer it is being asked to reproduce.
    """

    instant: datetime
    timezone_name: str = "UTC"
    #: How far back retrieval may reach. ``None`` means "no limit".
    lookback: timedelta | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instant", _as_aware(self.instant))
        if self.lookback is not None and self.lookback.total_seconds() < 0:
            raise InputError("as_of.lookback must not be negative", "回看时间不能为负")

    @property
    def window_start(self) -> datetime | None:
        return None if self.lookback is None else self.instant - self.lookback

    def allows(self, occurred_at: datetime) -> bool:
        """True when ``occurred_at`` is inside the permitted knowledge window."""
        moment = _as_aware(occurred_at)
        if moment > self.instant:
            return False
        start = self.window_start
        return start is None or moment >= start

    def to_dict(self) -> dict[str, Any]:
        return {
            "instant": self.instant.isoformat(),
            "timezone_name": self.timezone_name,
            "lookback_seconds": None if self.lookback is None else self.lookback.total_seconds(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AsOf":
        try:
            instant = datetime.fromisoformat(str(data.get("instant", "")).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise InputError("as_of.instant must be an ISO timestamp", "时间边界无法识别") from exc
        seconds = data.get("lookback_seconds")
        return cls(
            instant=instant,
            timezone_name=str(data.get("timezone_name", "UTC")),
            lookback=None if seconds is None else timedelta(seconds=float(seconds)),
        )


@dataclass(frozen=True)
class ContextEntry:
    """One retrieved fact, with the provenance a reviewer needs to trust it."""

    key: str
    value: Any
    #: Where this came from: ``"customer_profile"``, ``"order_history"``, ...
    source: str
    occurred_at: datetime | None = None
    #: True when a human -- not a model -- established this fact.
    confirmed: bool = False
    #: Identity of the fact it replaces, for memory supersession.
    supersedes: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise InputError("context entry requires a key")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise InputError("context entry confidence must be within 0..1")
        if self.occurred_at is not None:
            object.__setattr__(self, "occurred_at", _as_aware(self.occurred_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "occurred_at": None if self.occurred_at is None else self.occurred_at.isoformat(),
            "confirmed": self.confirmed,
            "supersedes": self.supersedes,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextEntry":
        raw_time = data.get("occurred_at")
        occurred = None
        if raw_time:
            occurred = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        return cls(
            key=str(data.get("key", "")),
            value=data.get("value"),
            source=str(data.get("source", "")),
            occurred_at=occurred,
            confirmed=bool(data.get("confirmed", False)),
            supersedes=str(data.get("supersedes", "")),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class ContextBundle:
    """Everything the backend is allowed to know for this run."""

    entries: tuple[ContextEntry, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries or ()))
        if len(self.entries) > MAX_CONTEXT_ENTRIES:
            raise InputError(f"context exceeds {MAX_CONTEXT_ENTRIES} entries", "上下文条目过多")

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def by_source(self, source: str) -> tuple[ContextEntry, ...]:
        return tuple(entry for entry in self.entries if entry.source == source)

    def keys(self) -> tuple[str, ...]:
        return tuple(entry.key for entry in self.entries)

    def merge(self, other: "ContextBundle") -> "ContextBundle":
        return ContextBundle(self.entries + tuple(other.entries))

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [entry.to_dict() for entry in self.entries]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextBundle":
        return cls(tuple(ContextEntry.from_dict(item) for item in data.get("entries", ()) if isinstance(item, Mapping)))


@dataclass(frozen=True)
class Policy:
    """Business policy knobs the host chooses; the core never invents them."""

    name: str
    version: str = "1"
    allow_price_pending: bool = False
    allow_partial_context: bool = False
    #: How many times the core may ask the host for more context in one run.
    max_context_continuations: int = 1
    flags: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise InputError("policy.name is required")
        if self.max_context_continuations < 0:
            raise InputError("policy.max_context_continuations must not be negative")
        object.__setattr__(self, "flags", dict(self.flags or {}))
        if len(self.flags) > MAX_POLICY_KEYS:
            raise InputError("policy.flags is unreasonably large", "策略参数过多")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "allow_price_pending": self.allow_price_pending,
            "allow_partial_context": self.allow_partial_context,
            "max_context_continuations": self.max_context_continuations,
            "flags": dict(self.flags),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Policy":
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "1")),
            allow_price_pending=bool(data.get("allow_price_pending", False)),
            allow_partial_context=bool(data.get("allow_partial_context", False)),
            max_context_continuations=int(data.get("max_context_continuations", 1)),
            flags=data.get("flags") or {},
        )


@dataclass(frozen=True)
class Budget:
    """Resource ceiling for one run.

    ``max_steps`` bounds the agent loop, ``timeout_seconds`` bounds wall clock,
    and ``max_input_tokens``/``max_output_tokens`` let a host with a direct
    model key stop a runaway from exceeding its own plan.
    """

    timeout_seconds: float = 60.0
    max_steps: int = 4
    max_input_tokens: int = 200_000
    max_output_tokens: int = 8_000
    max_cost_units: int = 1

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise InputError("budget.timeout_seconds must be positive", "执行时限必须大于零")
        if self.max_steps < 1:
            raise InputError("budget.max_steps must be at least 1", "步数上限必须大于零")
        if self.max_output_tokens < 1:
            raise InputError("budget.max_output_tokens must be positive", "输出上限必须大于零")
        if self.max_cost_units < 0:
            raise InputError("budget.max_cost_units must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_steps": self.max_steps,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_units": self.max_cost_units,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Budget":
        return cls(
            timeout_seconds=float(data.get("timeout_seconds", 60.0)),
            max_steps=int(data.get("max_steps", 4)),
            max_input_tokens=int(data.get("max_input_tokens", 200_000)),
            max_output_tokens=int(data.get("max_output_tokens", 8_000)),
            max_cost_units=int(data.get("max_cost_units", 1)),
        )


@dataclass(frozen=True)
class AgentRequest:
    """The single normalised input to a business agent run."""

    request_id: str
    idempotency_key: str
    task_type: str
    scope: Scope
    events: tuple[Event, ...]
    as_of: AsOf
    schema_version: str = CONTRACT_VERSION
    context: ContextBundle = field(default_factory=ContextBundle)
    policy: Policy = field(default_factory=lambda: Policy(name="default"))
    budget: Budget = field(default_factory=Budget)
    #: Free-form, host-supplied routing hints. Never interpreted by the core.
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.request_id).strip():
            raise InputError("request_id is required", "缺少任务编号")
        if not str(self.idempotency_key).strip():
            raise InputError("idempotency_key is required", "缺少幂等键")
        if not str(self.task_type).strip():
            raise InputError("task_type is required", "缺少任务类型")
        events = tuple(self.events or ())
        if not events:
            raise InputError("at least one event is required", "没有需要处理的内容")
        if len(events) > MAX_EVENTS:
            raise InputError(f"at most {MAX_EVENTS} events per request", "本次内容过多")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "attributes", dict(self.attributes or {}))
        # A run may never look past its own knowledge cut-off.
        future = [event for event in events if not self.as_of.allows(event.occurred_at)]
        if future:
            raise InputError(
                f"event {future[0].event_id} occurred after as_of", "内容时间晚于处理时点，无法处理"
            )

    @property
    def primary_event(self) -> Event:
        return self.events[0]

    def with_context(self, context: ContextBundle) -> "AgentRequest":
        return replace(self, context=context)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "task_type": self.task_type,
            "scope": self.scope.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "as_of": self.as_of.to_dict(),
            "context": self.context.to_dict(),
            "policy": self.policy.to_dict(),
            "budget": self.budget.to_dict(),
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentRequest":
        return cls(
            request_id=str(data.get("request_id", "")),
            idempotency_key=str(data.get("idempotency_key", "")),
            task_type=str(data.get("task_type", "")),
            scope=Scope.from_dict(data.get("scope") or {}),
            events=tuple(Event.from_dict(item) for item in data.get("events", ())),
            as_of=AsOf.from_dict(data.get("as_of") or {}),
            schema_version=str(data.get("schema_version", CONTRACT_VERSION)),
            context=ContextBundle.from_dict(data.get("context") or {}),
            policy=Policy.from_dict(data.get("policy") or {}),
            budget=Budget.from_dict(data.get("budget") or {}),
            attributes=data.get("attributes") or {},
        )


def merge_evidence_quotes(values: Sequence[Any]) -> list[str]:
    """Small helper used by plugins to normalise evidence quote lists."""
    return [str(value) for value in values if str(value).strip()]
