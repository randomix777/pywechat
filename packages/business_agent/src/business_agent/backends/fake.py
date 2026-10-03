"""Deterministic backend for tests, demos and offline development.

This is **not a model** and always reports ``model_executed = False``. Its whole
purpose is to exercise the core's queue, budget, cancellation, validation and
idempotency paths without an upstream provider, so that a passing test is never
mistaken for evidence about model quality.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from ..contracts.errors import AgentError, ErrorCategory
from ..contracts.protocols import BackendResult, PromptPackage
from ..contracts.request import AgentRequest


class FakeBackend:
    """Scripted answers, with scriptable failures.

    ``responses`` is either a list consumed in order, or a callable that
    receives ``(prompt, request)`` and returns the JSON answer. ``failures`` is
    consumed in the same order and lets a test force any error category.
    """

    name = "fake"
    model = ""
    is_model = False

    def __init__(
        self,
        responses: Sequence[Any] | Callable[[PromptPackage, AgentRequest], Any] | None = None,
        failures: Sequence[AgentError] | None = None,
        default_response: Any = None,
    ):
        self._responses = list(responses) if responses is not None and not callable(responses) else responses
        self._failures = list(failures or ())
        self._default = default_response
        #: Every prompt this backend received, for assertions on context shape.
        self.calls: list[PromptPackage] = []
        self.call_count = 0

    def run(self, prompt: PromptPackage, request: AgentRequest) -> BackendResult:
        self.call_count += 1
        self.calls.append(prompt)
        if self._failures:
            error = self._failures.pop(0)
            if error is not None:
                raise error
        answer = self._next_answer(prompt, request)
        if callable(answer) and not isinstance(answer, (dict, list)):
            answer = answer(prompt, request)
        if answer is None:
            answer = self._default
        if answer is None:
            raise AgentError(ErrorCategory.OUTPUT_INVALID, "fake backend has no scripted response")
        return BackendResult(
            text=answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False),
            input_tokens=0,
            output_tokens=0,
            handle=f"fake-session-{self.call_count}",
            model_executed=False,
            raw=answer if isinstance(answer, dict) else {},
        )

    def _next_answer(self, prompt: PromptPackage, request: AgentRequest) -> Any:
        if callable(self._responses):
            return self._responses(prompt, request)
        if self._responses:
            return self._responses.pop(0)
        return None

    def probe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "ok": True,
            "is_model": False,
            "checks": {"scripted_responses_remaining": len(self._responses) if not callable(self._responses) else -1},
        }
