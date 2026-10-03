"""A scope-bound, allow-listed tool registry.

The rule this enforces is the one that actually matters for a multi-merchant
deployment: **the model may not choose whose data it reads.**  A tool's scope
comes from the host that wired it; arguments that try to name a different
tenant, user or conversation are rejected before the handler ever runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..contracts.errors import ConfigError, InputError
from ..contracts.protocols import ToolHandler
from ..contracts.request import Scope

#: Argument names a model must never be able to use to widen access.
RESERVED_SCOPE_KEYS = frozenset(
    {
        "tenant_id",
        "tenant",
        "merchant_id",
        "merchant",
        "account_id",
        "user_id",
        "scope",
        "scope_id",
        "authorization",
        "token",
        "bearer",
    }
)

#: Argument names implying a side effect. Read-only registries refuse them.
FORBIDDEN_TOOL_KEYS = frozenset(
    {
        "sql",
        "query",
        "raw_sql",
        "command",
        "shell",
        "cmd",
        "exec",
        "path",
        "file",
        "filename",
        "url",
        "endpoint",
        "delete",
        "drop",
        "truncate",
        "update",
        "insert",
    }
)

#: Keys a tool may legitimately accept for paging/bounding.
ALLOWED_TOOL_KEYS = frozenset({"limit", "offset", "before", "after", "as_of", "query", "kind", "fields"})


@dataclass(frozen=True)
class ToolSpec:
    """What a backend is told about one tool."""

    name: str
    description: str
    #: JSON-schema-ish parameters. Kept loose on purpose: a real host may want
    #: stricter, but the registry must never *infer* authority from it.
    parameters: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": dict(self.parameters)}


class ScopedToolRegistry:
    """Allow-listed tools, permanently bound to one scope.

    Constructed by the trusted host. The model only ever supplies arguments.
    """

    def __init__(self, scope: Scope, tools: Mapping[str, ToolHandler] | None = None, max_limit: int = 50):
        self.scope = scope
        self.max_limit = int(max_limit)
        if self.max_limit < 1:
            raise ConfigError("tool registry max_limit must be positive")
        self._tools: dict[str, ToolHandler] = dict(tools or {})

    # ------------------------------------------------------------- registry

    def register(self, name: str, handler: ToolHandler, description: str = "", parameters: Mapping[str, Any] | None = None) -> None:
        """Add a tool. The host calls this; the model never can."""
        if not str(name).strip():
            raise ConfigError("tool name must not be empty")
        if name in self._tools:
            raise ConfigError(f"tool {name} is already registered")
        self._tools[name] = handler

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def describe(self, name: str) -> Mapping[str, Any]:
        if name not in self._tools:
            raise InputError(f"unknown tool: {name}", "请求的能力不存在")
        description = getattr(self._tools[name], "description", "") or ""
        parameters = getattr(self._tools[name], "parameters", None) or {}
        return ToolSpec(name=name, description=str(description), parameters=dict(parameters)).to_dict()

    def catalogue(self) -> list[dict[str, Any]]:
        """Every available tool, for inclusion in a prompt."""
        return [self.describe(name) for name in self.names()]

    # ------------------------------------------------------------- invoking

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None, scope: Scope | None = None) -> Any:
        """Run a whitelisted tool with host-bound scope.

        ``scope`` may only be omitted or equal to the bound scope. Passing a
        different one is an attack, not a configuration error.
        """
        if name not in self._tools:
            raise InputError(f"tool is not available: {name}", "请求的能力不存在")
        effective = scope or self.scope
        if effective != self.scope:
            raise InputError("a tool may not be invoked for a different scope", "无权访问该数据")
        args = self._check_arguments(arguments or {})
        return self._tools[name](args, self.scope)

    def _check_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise InputError("tool arguments must be an object", "请求的内容无法处理")
        cleaned: dict[str, Any] = {}
        for key, value in arguments.items():
            name = str(key)
            lowered = name.lower()
            if lowered in RESERVED_SCOPE_KEYS:
                raise InputError(
                    f"tool argument {name} may not set the business scope", "无权访问该数据"
                )
            if lowered in FORBIDDEN_TOOL_KEYS:
                raise InputError(f"tool argument {name} is not permitted", "请求的内容无法处理")
            if lowered not in ALLOWED_TOOL_KEYS:
                raise InputError(f"tool argument {name} is not recognised", "请求的内容无法处理")
            cleaned[name] = value
        if "limit" in cleaned:
            try:
                limit = int(cleaned["limit"])
            except (TypeError, ValueError) as exc:
                raise InputError("tool limit must be an integer", "请求的内容无法处理") from exc
            if limit < 1 or limit > self.max_limit:
                # Clamp rather than reject: a model asking for 10 000 rows is
                # miscalibrated, not malicious, and a clamped answer is still safe.
                cleaned["limit"] = self.max_limit
        return cleaned


class UnavailableToolRegistry:
    """A registry with no tools, for hosts that only use pre-queried context.

    Present so the core can depend on one interface instead of branching on
    ``None`` for every tool call.
    """

    scope: Scope | None = None

    def names(self) -> tuple[str, ...]:
        return ()

    def describe(self, name: str) -> Mapping[str, Any]:
        raise InputError(f"tool is not available: {name}", "请求的能力不存在")

    def catalogue(self) -> list[dict[str, Any]]:
        return []

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None, scope: Scope | None = None) -> Any:
        raise InputError("no tools are available in this run", "请求的能力不存在")
