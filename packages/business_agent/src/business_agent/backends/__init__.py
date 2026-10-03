"""Execution backends.

Three choices, and the difference between them matters:

* :class:`~business_agent.backends.opencode.OpenCodeBackend` -- the cloud path.
  OpenCode owns its internal agent loop; the core submits one restricted task
  and does not nest another loop around it.
* :class:`~business_agent.backends.model_api.ModelApiBackend` -- for a host that
  holds its own model key. No inner loop, so the core's step budget is the
  bound.
* :class:`~business_agent.backends.fake.FakeBackend` -- deterministic, and
  never claims to be a model.
"""
from __future__ import annotations

from .fake import FakeBackend
from .model_api import ModelApiBackend
from .opencode import (
    PINNED_OPENCODE_VERSION,
    REQUIRED_DENIED_PERMISSIONS,
    REQUIRED_ENDPOINTS,
    OpenCodeBackend,
    render_prompt,
)

__all__ = [
    "FakeBackend",
    "ModelApiBackend",
    "OpenCodeBackend",
    "PINNED_OPENCODE_VERSION",
    "REQUIRED_DENIED_PERMISSIONS",
    "REQUIRED_ENDPOINTS",
    "render_prompt",
]
