"""The generic agent core.

What the core does, in order:

1. validate the request contract and the domain plugin's own input rules;
2. short-circuit on a prior run with the same idempotency key;
3. assemble context under the run's knowledge cut-off;
4. hand a plugin-built prompt to an execution backend, with a bounded step
   budget, wall-clock timeout and cancellation;
5. validate the structured result, then let the plugin interpret it;
6. record run state, evidence, usage and error category.

What the core deliberately never does: write an order, send a message, mutate
a customer's long-term facts, charge anybody, or decide who is allowed in.
Those are the host's, after it has applied its own business rules.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence
from uuid import uuid4

from ..contracts.errors import (
    RETRYABLE_CATEGORIES,
    UNSETTLED_CATEGORIES,
    AgentError,
    BusinessAmbiguity,
    Cancelled,
    ConfigError,
    ErrorCategory,
    InputError,
    OutputInvalid,
)
from ..contracts.protocols import (
    BackendResult,
    ContextProvider,
    DomainPlugin,
    ExecutionBackend,
    MemoryReader,
    RunRepository,
    ToolRegistry,
)
from ..contracts.request import AgentRequest, ContextBundle
from ..contracts.result import AgentResult, RunStatus, Usage, Versions
from .context import ContextAssembler
from .runs import InMemoryRunRepository
from .validation import extract_json_object

#: How many times a *retryable* backend failure may be re-attempted inside one
#: run. Deliberately small: the host's queue owns long-term retry with
#: backoff, and two retry loops in series is how a merchant gets billed twice.
DEFAULT_MAX_ATTEMPTS = 2


class RunCancelled(Exception):
    """Raised internally when a cancellation token fires."""


class CancellationToken:
    """Cooperative cancellation plus a deadline."""

    def __init__(self, event: threading.Event | None = None, deadline: float | None = None, clock: Callable[[], float] = time.monotonic):
        self._event = event or threading.Event()
        self._deadline = deadline
        self._clock = clock

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def expired(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline

    def check(self) -> None:
        if self.cancelled:
            raise RunCancelled("cancelled by host")
        if self.expired:
            raise AgentError(ErrorCategory.TIMEOUT, "run exceeded its wall-clock budget")

    def remaining(self) -> float:
        if self._deadline is None:
            return float("inf")
        return max(0.0, self._deadline - self._clock())


class AgentCore:
    """Domain-neutral agent runtime.

    ``plugin`` supplies everything business-specific; ``backend`` decides how the
    prompt is actually executed. Swapping in a second domain means writing a new
    plugin, not touching this class.
    """

    def __init__(
        self,
        backend: ExecutionBackend,
        plugin: DomainPlugin,
        context_providers: Sequence[ContextProvider] = (),
        memory_reader: MemoryReader | None = None,
        tools: ToolRegistry | None = None,
        run_repository: RunRepository | None = None,
        base_context: ContextBundle | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        id_factory: Callable[[], str] | None = None,
    ):
        if max_attempts < 1:
            raise ConfigError("max_attempts must be at least 1")
        self.backend = backend
        self.plugin = plugin
        self.memory_reader = memory_reader
        self.tools = tools
        self.runs = run_repository if run_repository is not None else InMemoryRunRepository()
        self.context = ContextAssembler(context_providers, base_context)
        self.max_attempts = int(max_attempts)
        self.retry_backoff = float(retry_backoff)
        self._clock = clock
        self._sleep = sleep
        self._id = id_factory or (lambda: uuid4().hex)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ run

    def run(self, request: AgentRequest, token: CancellationToken | None = None) -> AgentResult:
        """Execute one request and always return a result.

        This method does not raise for business or backend failures: a caller
        that gets an exception back cannot tell "the model was unsure" from
        "the network broke", which is exactly the distinction that gets
        mis-handled.
        """
        run_id = self._id()
        deadline = self._clock() + request.budget.timeout_seconds
        token = token or CancellationToken(deadline=deadline, clock=self._clock)
        started = self._clock()
        result: AgentResult | None = None

        try:
            result = self._run_inner(request, token, run_id, deadline, started)
        except RunCancelled as exc:
            result = self._failure(request, run_id, Cancelled(str(exc)), Usage(elapsed_seconds=self._clock() - started))
        except AgentError as exc:
            usage = Usage(elapsed_seconds=self._clock() - started)
            result = self._failure(request, run_id, exc, usage)
        except Exception as exc:  # pragma: no cover - defensive
            result = self._failure(
                request, run_id, AgentError(ErrorCategory.CONFIG, f"unhandled {type(exc).__name__}: {exc}"), Usage()
            )
        assert result is not None
        return result

    def _run_inner(
        self, request: AgentRequest, token: CancellationToken, run_id: str, deadline: float, started: float
    ) -> AgentResult:
        # 1. Contract + domain input validation.
        if request.task_type not in self.plugin.supported_tasks:
            raise InputError(
                f"task {request.task_type} is not supported by plugin {self.plugin.name}", "暂不支持该任务"
            )
        self.plugin.validate_request(request)

        # 2. Idempotency: a settled prior run answers this request for free.
        with self._lock:
            prior = self.runs.find_by_idempotency_key(request.scope, request.idempotency_key)
        if prior is not None:
            return prior

        # 3. Context, bounded by the knowledge cut-off.
        bundle = self.context.build(request)
        if self.memory_reader is not None:
            bundle = bundle.merge(self.memory_reader.recall(request))

        # 4. Execute with a bounded step budget and a hard deadline.
        token.check()
        prompt = self.plugin.build_prompt(request, bundle)
        backend_result, attempts = self._execute(prompt, request, token, deadline)

        # 5. Validate the structured answer, then interpret it.
        payload = self._to_payload(backend_result)
        validated = self.plugin.validate_output(payload, request)
        interpretation = self.plugin.interpret(validated, request)

        return self._succeed(request, run_id, backend_result, validated, interpretation, attempts, started)

    # ------------------------------------------------------------- execution

    def _execute(
        self, prompt: Any, request: AgentRequest, token: CancellationToken, deadline: float
    ) -> tuple[BackendResult, int]:
        """Run the backend, retrying only what is safe to retry.

        ``network`` and ``rate_limited`` are retried because nothing was
        executed. ``outcome_unknown`` and ``timeout`` are **not**: the prompt
        already reached the backend, so a retry may execute the same work twice
        and bill the merchant twice.
        """
        attempt = 0
        while True:
            token.check()
            attempt += 1
            try:
                return self.backend.run(prompt, request), attempt
            except AgentError as exc:
                if exc.category in UNSETTLED_CATEGORIES:
                    raise
                if exc.category not in RETRYABLE_CATEGORIES or attempt >= self.max_attempts:
                    raise
                self._sleep(min(self.retry_backoff * attempt, 2.0))
            except RunCancelled:
                raise

    @staticmethod
    def _to_payload(backend_result: BackendResult) -> dict[str, Any]:
        """Turn a backend answer into a mapping, or fail loudly.

        A backend that already parsed JSON may hand back ``raw``; everything
        else goes through strict extraction.
        """
        if isinstance(backend_result.raw, dict) and backend_result.raw:
            return dict(backend_result.raw)
        return extract_json_object(backend_result.text)

    # --------------------------------------------------------------- results

    def _succeed(
        self,
        request: AgentRequest,
        run_id: str,
        backend_result: BackendResult,
        output: dict[str, Any],
        interpretation: Any,
        attempts: int,
        started: float,
    ) -> AgentResult:
        blocking = tuple(item for item in interpretation.ambiguities if getattr(item, "blocking", True))
        if interpretation.requires_more_context:
            status = RunStatus.NEEDS_CONTEXT
        elif blocking:
            status = RunStatus.NEEDS_REVIEW
        else:
            status = RunStatus.SUCCEEDED
        usage = Usage(
            input_tokens=backend_result.input_tokens,
            output_tokens=backend_result.output_tokens,
            steps=attempts,
            cost_units=request.budget.max_cost_units if status != RunStatus.FAILED else 0,
            model_executed=backend_result.model_executed,
            elapsed_seconds=self._clock() - started,
        )
        result = AgentResult(
            run_id=run_id,
            request_id=request.request_id,
            status=status,
            output=dict(output),
            evidence=tuple(self._verify_evidence(interpretation, request)),
            ambiguities=tuple(interpretation.ambiguities),
            suggested_actions=tuple(interpretation.suggested_actions),
            memory_proposals=tuple(interpretation.memory_proposals),
            missing_information=tuple(interpretation.missing_information),
            usage=usage,
            versions=self._versions(),
            merchant_message=("需要您确认" if status == RunStatus.NEEDS_REVIEW else ""),
        )
        self._persist(result, request)
        return result

    def _failure(self, request: AgentRequest, run_id: str, error: AgentError, usage: Usage) -> AgentResult:
        status = (
            RunStatus.OUTCOME_UNKNOWN
            if error.category in UNSETTLED_CATEGORIES
            else RunStatus.CANCELLED
            if error.category == ErrorCategory.CANCELLED
            else RunStatus.FAILED
        )
        # A failed run carries an error *category*, not an ambiguity. The two
        # are reported differently on purpose: mixing them hides a real
        # infrastructure fault behind a familiar-looking review card, and an
        # ambiguity is a normal outcome that must never look like an outage.
        result = AgentResult(
            run_id=run_id,
            request_id=request.request_id,
            status=status,
            usage=usage,
            versions=self._versions(),
            error_category=error.category,
            error_detail=error.detail,
            merchant_message=error.merchant_message,
        )
        self._persist(result, request)
        return result

    def _persist(self, result: AgentResult, request: AgentRequest) -> None:
        with self._lock:
            recorder = getattr(self.runs, "record", None)
            if callable(recorder):
                recorder(result.run_id, request)
            self.runs.finish(result)

    def _versions(self) -> Versions:
        return Versions(
            backend=str(getattr(self.backend, "name", "") or ""),
            model=str(getattr(self.backend, "model", "") or ""),
            prompt_version=str(getattr(self.plugin, "prompt_version", "") or ""),
            policy_version="",
            plugin_version=str(getattr(self.plugin, "version", "") or ""),
        )

    @staticmethod
    def _verify_evidence(interpretation: Any, request: AgentRequest) -> list[Any]:
        """Mark evidence quotes that actually appear in the submitted input.

        An unverified quote is still returned -- hiding it would lose the
        reviewer's only clue -- but it is flagged so a host can refuse to apply
        a result that cites text nobody said.
        """
        haystack = "\n".join(event.body for event in request.events)
        verified: list[Any] = []
        for item in getattr(interpretation, "evidence", ()) or ():
            quote = getattr(item, "quote", "")
            try:
                verified.append(
                    type(item)(
                        ref=item.ref,
                        quote=quote,
                        source=getattr(item, "source", ""),
                        verified=quote in haystack,
                    )
                )
            except TypeError:  # pragma: no cover - custom evidence types
                verified.append(item)
        return verified
