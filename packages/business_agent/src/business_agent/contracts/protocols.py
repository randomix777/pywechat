"""The seams a business agent is assembled from.

Each protocol below marks a *real* replacement point -- something a host
genuinely swaps out.  There is deliberately no abstract class per function:
over-abstracting stable code makes it harder to read, not more extensible.

Dependency direction::

    product integration  ->  domain plugin  ->  business_agent.core
                                                     |
                                                     v
                                              ExecutionBackend
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .errors import AgentError
from .request import AgentRequest, ContextBundle, ContextEntry, Scope
from .result import AgentResult


@dataclass(frozen=True)
class PromptPackage:
    """What the core hands to a backend.

    ``system`` is the only instruction channel; ``payload`` carries the run as
    inert data.  Backends must never concatenate user text into ``system``.
    """

    system: str
    payload: Mapping[str, Any]
    max_output_tokens: int = 4000
    #: Opaque per-run label; backends use it for session titles and logs.
    run_label: str = ""


@dataclass(frozen=True)
class BackendResult:
    """Raw backend answer before plugin validation."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: Handle the host needs to reconcile or clean up (e.g. an upstream session id).
    handle: str = ""
    #: True when a real model produced this. Stubs must report ``False``.
    model_executed: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: ``provider/model`` the upstream says it actually ran, as reported in the
    #: reply. Empty when the upstream does not return an identifiable model —
    #: callers must treat that as *unconfirmed*, never as agreement.
    model_reported: str = ""
    model_reported_provider: str = ""
    model_reported_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "handle": self.handle,
            "model_executed": self.model_executed,
        }


@runtime_checkable
class ExecutionBackend(Protocol):
    """Runs one prompt to completion and returns text.

    A backend owns its own internal agent loop if it has one; the core never
    wraps a backend that loops internally in a retry loop of its own, which is
    how runaway double-charging happens.
    """

    #: Stable identifier recorded in every result.
    name: str

    def run(self, prompt: PromptPackage, request: AgentRequest) -> BackendResult:
        """Execute ``prompt`` and return the raw answer.

        Must raise :class:`AgentError` with a meaningful category on failure --
        in particular :data:`~business_agent.contracts.errors.ErrorCategory.OUTCOME_UNKNOWN`
        when the prompt was already submitted but no answer came back.
        """

    def probe(self) -> dict[str, Any]:
        """Report live health. Used by operator tooling; never gates a run."""


@runtime_checkable
class ContextProvider(Protocol):
    """Supplies business context for a run, honouring its knowledge cut-off."""

    def collect(self, request: AgentRequest) -> ContextBundle:
        """Return context that is valid as of ``request.as_of``.

        Implementations must not return facts that only became true after the
        cut-off; the core re-checks ``occurred_at`` but cannot check semantics.
        """

    def describe_missing(self, request: AgentRequest) -> Sequence[str]:
        """What this provider would need in order to be more useful."""


@runtime_checkable
class MemoryReader(Protocol):
    """Read-only access to long-term business facts for one scope."""

    def recall(self, request: AgentRequest, keys: Sequence[str] = ()) -> ContextBundle:
        """Return facts bounded by ``request.as_of``."""


@runtime_checkable
class ToolHandler(Protocol):
    """A read-only business lookup, bound to one scope by the host.

    Handlers receive the host-bound :class:`Scope`; they must never accept a
    scope override from their arguments.
    """

    name: str

    def __call__(self, arguments: Mapping[str, Any], scope: Scope) -> Any:
        """Perform the lookup and return JSON-serialisable data."""


@runtime_checkable
class ToolRegistry(Protocol):
    """The only tools a run may call. Anything absent is denied."""

    def names(self) -> tuple[str, ...]:
        """Tool names available for this run."""

    def describe(self, name: str) -> Mapping[str, Any]:
        """JSON-schema-ish description passed to the model."""

    def invoke(self, name: str, arguments: Mapping[str, Any], scope: Scope) -> Any:
        """Run a tool. Must reject unknown names and scope-widening arguments."""


@runtime_checkable
class DomainPlugin(Protocol):
    """Everything that makes one business domain different from another.

    This is the only place order rules, order vocabulary or order output shapes
    are allowed to live.  The generic core must not grow any of it.
    """

    name: str
    version: str
    prompt_version: str
    supported_tasks: tuple[str, ...]

    def validate_request(self, request: AgentRequest) -> None:
        """Domain-specific input checks. Raise :class:`InputError` on failure."""

    def build_prompt(self, request: AgentRequest, context: ContextBundle) -> PromptPackage:
        """Render the system prompt and the inert data payload."""

    def validate_output(self, payload: Mapping[str, Any], request: AgentRequest) -> Mapping[str, Any]:
        """Structurally validate the model's JSON. Raise on failure.

        Must not make business decisions -- that is the core's post-validation
        step, driven by what this plugin reports.
        """

    def interpret(self, output: Mapping[str, Any], request: AgentRequest) -> Interpretation:
        """Turn validated output into ambiguities, actions and memory proposals."""


@dataclass(frozen=True)
class Interpretation:
    """What a plugin concludes from its own validated output.

    ``blocking`` ambiguities move the run to ``needs_review``.  ``requires_more_context``
    moves it to ``needs_context`` so the host can supply missing context a bounded
    number of times instead of the run guessing.
    """

    evidence: tuple[Any, ...] = ()
    ambiguities: tuple[Any, ...] = ()
    suggested_actions: tuple[Any, ...] = ()
    memory_proposals: tuple[Any, ...] = ()
    missing_information: tuple[str, ...] = ()
    requires_more_context: bool = False


@runtime_checkable
class RunRepository(Protocol):
    """Durable record of run status, evidence and usage."""

    def start(self, request: AgentRequest) -> str:
        """Begin a run and return its run id."""

    def finish(self, result: AgentResult) -> None:
        """Persist the terminal outcome."""

    def get(self, run_id: str) -> AgentResult | None:
        """Read a run back, or ``None`` when unknown."""

    def find_by_idempotency_key(self, scope: Scope, idempotency_key: str) -> AgentResult | None:
        """Look up a prior run so a retried request never charges twice."""


#: Convenience alias for the whole wiring set.
AgentAssembly = Mapping[str, Any]

__all__ = [
    "AgentAssembly",
    "BackendResult",
    "ContextProvider",
    "DomainPlugin",
    "ExecutionBackend",
    "Interpretation",
    "MemoryReader",
    "PromptPackage",
    "RunRepository",
    "ToolHandler",
    "ToolRegistry",
    "ContextEntry",
    "AgentError",
    "AgentResult",
    "Callable",
]
