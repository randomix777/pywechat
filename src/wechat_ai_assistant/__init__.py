"""WeChat AI Assistant application package.

The package keeps the historical automation API available at the top level
while importing Windows UI dependencies only when those names are requested.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

from .compat import import_legacy_environment

import_legacy_environment()

_EXPORTS = {
    "AutoReply": ".WeChatAuto", "Collections": ".WeChatAuto", "Call": ".WeChatAuto",
    "Contacts": ".WeChatAuto", "Files": ".WeChatAuto", "FriendSettings": ".WeChatAuto",
    "Messages": ".WeChatAuto", "Moments": ".WeChatAuto", "Monitor": ".WeChatAuto",
    "Settings": ".WeChatAuto", "Tools": ".WeChatTools", "Navigator": ".WeChatTools",
    "SystemSettings": ".WinSettings", "GlobalConfig": ".Config",
    "MessageDirection": ".reliability", "MessageEvent": ".reliability",
    "MessageKind": ".reliability", "MessageLedger": ".reliability",
    "OperationQueueClosed": ".reliability", "OperationQueueFull": ".reliability",
    "UiOperationQueue": ".reliability", "retry_call": ".reliability",
    "AgentCommand": ".agent", "AgentConfig": ".agent", "AgentStore": ".agent",
    "LocalAgent": ".agent", "CloudClient": ".cloud", "CloudError": ".cloud",
    "CloudResponse": ".cloud", "CloudRequestHandler": ".cloud_server",
    "CloudStore": ".cloud_server", "create_server": ".cloud_server",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
