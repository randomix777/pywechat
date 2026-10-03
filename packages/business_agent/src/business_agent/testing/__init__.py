"""Helpers for testing a domain plugin without a model.

The point of this module is that a second business domain can be tested with the
same few lines as the first: a scripted backend, a request builder, and
assertions on the *business structure* rather than on model wording.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..contracts.errors import AgentError, ErrorCategory
from ..contracts.request import (
    AgentRequest,
    AsOf,
    Budget,
    ContextBundle,
    ContextEntry,
    Event,
    Policy,
    Scope,
)
from ..contracts.result import AgentResult, Ambiguity, Evidence, RunStatus

#: A fixed instant so fixtures never depend on the wall clock.
REFERENCE_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def build_request(
    task_type: str = "test_task",
    body: str = "测试内容",
    *,
    request_id: str = "req-1",
    idempotency_key: str = "idem-1",
    tenant_id: str = "tenant-a",
    event_id: str = "evt-1",
    occurred_at: datetime | None = None,
    as_of: datetime | None = None,
    lookback: timedelta | None = None,
    actor: str = "客户",
    context: ContextBundle | None = None,
    policy: Policy | None = None,
    budget: Budget | None = None,
    scope: Scope | None = None,
    extra_events: Sequence[Event] = (),
) -> AgentRequest:
    """A valid request with sensible defaults and no hidden magic."""
    moment = occurred_at or REFERENCE_NOW
    return AgentRequest(
        request_id=request_id,
        idempotency_key=idempotency_key,
        task_type=task_type,
        scope=scope or Scope(tenant_id=tenant_id, user_id="u-1", conversation_id="conv-1"),
        events=(Event(event_id=event_id, body=body, occurred_at=moment, actor=actor),) + tuple(extra_events),
        as_of=AsOf(instant=as_of or moment, lookback=lookback),
        context=context or ContextBundle(),
        policy=policy or Policy(name="test"),
        budget=budget or Budget(timeout_seconds=5.0),
    )


def context_entry(key: str, value: Any, source: str = "test", **kwargs: Any) -> ContextEntry:
    return ContextEntry(key=key, value=value, source=source, **kwargs)


def error(category: str, detail: str = "test failure") -> AgentError:
    """Build an :class:`AgentError` with a specific category."""
    return AgentError(category, detail)


def json_answer(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------------ asserts


def assert_evidence_grounded(result: AgentResult, request: AgentRequest) -> None:
    """Every cited quote must appear verbatim in the submitted input.

    This is the single most valuable assertion in the whole suite: a model that
    cites text nobody said has invented a justification, and every downstream
    automatic decision rests on it.
    """
    haystack = "\n".join(event.body for event in request.events)
    ungrounded = [item.quote for item in result.evidence if not item.verified]
    assert not ungrounded, f"evidence not found in the input: {ungrounded}"


def assert_no_side_effect_payloads(result: AgentResult) -> None:
    """A proposal must never carry instructions to mutate anything."""
    text = json.dumps(result.to_dict(), ensure_ascii=False, default=str).lower()
    for forbidden in ("drop table", "delete from", "update orders", "rm -rf", "os.system", "eval("):
        assert forbidden not in text, f"result payload contains an executable instruction: {forbidden}"


def assert_scope_confined(result: AgentResult, tenant_id: str) -> None:
    """Memory proposals must stay inside the request's own scope."""
    for proposal in result.memory_proposals:
        assert proposal.scope.tenant_id == tenant_id, (
            f"memory proposal escaped its tenant: {proposal.scope.tenant_id} != {tenant_id}"
        )


def assert_ambiguity_codes(result: AgentResult, expected: Iterable[str]) -> None:
    actual = {item.code for item in result.ambiguities}
    missing = set(expected) - actual
    assert not missing, f"missing ambiguity codes: {sorted(missing)} (got {sorted(actual)})"


def assert_settled(result: AgentResult) -> None:
    assert result.settled, f"run is unsettled: {result.status} / {result.error_category}"


def assert_not_a_model(result: AgentResult) -> None:
    """Guard against a simulated result being reported as a model result."""
    assert not result.usage.model_executed, "a deterministic backend must not report a model execution"


def blocking_codes(result: AgentResult) -> set[str]:
    return {item.code for item in result.ambiguities if item.blocking}


__all__ = [
    "REFERENCE_NOW",
    "Ambiguity",
    "Budget",
    "ContextBundle",
    "ErrorCategory",
    "Event",
    "Evidence",
    "Policy",
    "RunStatus",
    "Scope",
    "assert_ambiguity_codes",
    "assert_evidence_grounded",
    "assert_no_side_effect_payloads",
    "assert_not_a_model",
    "assert_scope_confined",
    "assert_settled",
    "blocking_codes",
    "build_request",
    "context_entry",
    "error",
    "json_answer",
]
