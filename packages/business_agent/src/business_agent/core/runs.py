"""Run persistence.

The core does not own a database. A host that needs durability supplies its own
:class:`~business_agent.contracts.protocols.RunRepository`; the in-memory one
here is what tests, demos and single-shot CLI runs use.

The idempotency lookup is the reason this exists at all: a retried request must
return the first run's result instead of executing again and being charged
twice.
"""
from __future__ import annotations

import threading
from typing import Iterator

from ..contracts.protocols import RunRepository
from ..contracts.request import AgentRequest, Scope
from ..contracts.result import AgentResult, RunStatus


class InMemoryRunRepository(RunRepository):
    """Thread-safe, process-local run store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._results: dict[str, AgentResult] = {}
        self._by_idempotency: dict[tuple[str, str], str] = {}
        self._requests: dict[str, AgentRequest] = {}

    def start(self, request: AgentRequest) -> str:
        del request  # The run id is minted by the runtime, not the repository.
        raise NotImplementedError("the runtime assigns the run id")

    def record(self, run_id: str, request: AgentRequest) -> None:
        """Bind a runtime-minted run id to its request."""
        with self._lock:
            self._requests[run_id] = request

    def finish(self, result: AgentResult) -> None:
        with self._lock:
            self._results[result.run_id] = result
            request = self._requests.get(result.run_id)
            if request is not None:
                key = (request.scope.tenant_id, request.idempotency_key)
                if result.status in (RunStatus.SUCCEEDED, RunStatus.NEEDS_REVIEW, RunStatus.NEEDS_CONTEXT):
                    # Only a settled, usable result may satisfy a retry. A
                    # failed or unknown run must be allowed to run again.
                    self._by_idempotency[key] = result.run_id
                elif key not in self._by_idempotency:
                    self._by_idempotency.setdefault(key, "")

    def get(self, run_id: str) -> AgentResult | None:
        with self._lock:
            return self._results.get(run_id)

    def find_by_idempotency_key(self, scope: Scope, idempotency_key: str) -> AgentResult | None:
        with self._lock:
            run_id = self._by_idempotency.get((scope.tenant_id, idempotency_key))
            return self._results.get(run_id) if run_id else None

    def request_for(self, run_id: str) -> AgentRequest | None:
        with self._lock:
            return self._requests.get(run_id)

    def __iter__(self) -> Iterator[AgentResult]:
        with self._lock:
            return iter(tuple(self._results.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._results)
