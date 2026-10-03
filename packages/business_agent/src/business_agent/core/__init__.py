"""Core wiring and validation. Business logic lives in a domain plugin."""
from __future__ import annotations

from .context import ContextAssembler
from .runs import InMemoryRunRepository
from .runtime import DEFAULT_MAX_ATTEMPTS, AgentCore, CancellationToken
from .tools import ScopedToolRegistry, ToolSpec, UnavailableToolRegistry
from .validation import (
    MAX_OUTPUT_BYTES,
    enum_field,
    extract_json_object,
    optional_number,
    optional_string,
    require_keys,
    require_string_list,
    verify_quotes,
    wrap_untrusted,
)

__all__ = [
    "AgentCore",
    "CancellationToken",
    "ContextAssembler",
    "DEFAULT_MAX_ATTEMPTS",
    "InMemoryRunRepository",
    "MAX_OUTPUT_BYTES",
    "ScopedToolRegistry",
    "ToolSpec",
    "UnavailableToolRegistry",
    "enum_field",
    "extract_json_object",
    "optional_number",
    "optional_string",
    "require_keys",
    "require_string_list",
    "verify_quotes",
    "wrap_untrusted",
]
