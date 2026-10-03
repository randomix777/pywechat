"""business_agent -- a reusable, business-neutral agent core.

What it is for: one run pipeline -- validate, assemble context under a knowledge
cut-off, execute against a replaceable backend, validate the structured result,
return a proposal with evidence -- that any business domain can plug into.

What it is **not**: it has no WeChat, no Windows UI, no SQLite schema of yours,
no login, no billing, and no fixed model provider. Anything that would be true
of only one business belongs in a plugin, not here.

Minimal use::

    from business_agent import AgentCore, FakeBackend
    from order_agent import OrderPlugin

    core = AgentCore(backend=FakeBackend(responses=[{...}]), plugin=OrderPlugin())
    result = core.run(request)
    if result.ok:
        ...  # the host decides what to do with result.suggested_actions
"""
from __future__ import annotations

from .backends import FakeBackend, ModelApiBackend, OpenCodeBackend
from .contracts import (
    CONTRACT_VERSION,
    RESULT_VERSION,
    AgentError,
    AgentRequest,
    AgentResult,
    Ambiguity,
    AsOf,
    BackendResult,
    Budget,
    ContextBundle,
    ContextEntry,
    ContextProvider,
    DomainPlugin,
    ErrorCategory,
    Event,
    Evidence,
    ExecutionBackend,
    InputError,
    Interpretation,
    MemoryProposal,
    MemoryReader,
    OutcomeUnknown,
    Policy,
    PromptPackage,
    RunRepository,
    RunStatus,
    Scope,
    SuggestedAction,
    Usage,
    Versions,
)
from .core import (
    AgentCore,
    CancellationToken,
    ContextAssembler,
    InMemoryRunRepository,
    ScopedToolRegistry,
    extract_json_object,
    verify_quotes,
    wrap_untrusted,
)

#: Package version. Bumped independently of any product release.
__version__ = "0.1.0"

__all__ = [
    "AgentCore",
    "AgentError",
    "AgentRequest",
    "AgentResult",
    "Ambiguity",
    "AsOf",
    "BackendResult",
    "Budget",
    "CONTRACT_VERSION",
    "CancellationToken",
    "ContextAssembler",
    "ContextBundle",
    "ContextEntry",
    "ContextProvider",
    "DomainPlugin",
    "ErrorCategory",
    "Event",
    "Evidence",
    "ExecutionBackend",
    "FakeBackend",
    "InMemoryRunRepository",
    "InputError",
    "Interpretation",
    "MemoryProposal",
    "MemoryReader",
    "ModelApiBackend",
    "OpenCodeBackend",
    "OutcomeUnknown",
    "Policy",
    "PromptPackage",
    "RESULT_VERSION",
    "RunRepository",
    "RunStatus",
    "Scope",
    "ScopedToolRegistry",
    "SuggestedAction",
    "Usage",
    "Versions",
    "__version__",
    "extract_json_object",
    "verify_quotes",
    "wrap_untrusted",
]
