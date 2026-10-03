"""Context assembly under a knowledge cut-off.

Two context styles are supported and they are genuinely different products:

* **ContextBundle** -- the host pre-queries and submits context. This is the
  WeChat product's default: the merchant's database never becomes a tool the
  model can aim at.
* **Bounded read-only tools** -- for hosts with a trustworthy data service
  behind them, where the model may ask precise follow-up questions.

Both are bounded the same way: nothing observed after ``as_of`` is ever allowed
through, no matter which path produced it.
"""
from __future__ import annotations

from typing import Sequence

from ..contracts.errors import InputError
from ..contracts.protocols import ContextProvider
from ..contracts.request import AgentRequest, ContextBundle, ContextEntry

#: Absolute ceiling on total context size handed to a backend.
MAX_CONTEXT_CHARS = 200_000


class ContextAssembler:
    """Collects context from providers and enforces the time boundary."""

    def __init__(self, providers: Sequence[ContextProvider] = (), base: ContextBundle | None = None):
        self.providers = tuple(providers or ())
        self.base = base or ContextBundle()

    def build(self, request: AgentRequest) -> ContextBundle:
        """Merge host-submitted and provider-supplied context, filtered by ``as_of``."""
        collected: list[ContextEntry] = [entry for entry in self.base]
        for provider in self.providers:
            bundle = provider.collect(request)
            if not isinstance(bundle, ContextBundle):
                raise InputError(
                    f"context provider {type(provider).__name__} did not return a ContextBundle",
                    "上下文服务返回了无法识别的内容",
                )
            collected.extend(bundle.entries)
        return self._enforce_boundary(request, ContextBundle(tuple(collected)))

    def missing_information(self, request: AgentRequest) -> list[str]:
        """What providers say they would need, de-duplicated and ordered."""
        missing: list[str] = []
        for provider in self.providers:
            for item in provider.describe_missing(request) or ():
                text = str(item).strip()
                if text and text not in missing:
                    missing.append(text)
        return missing

    @staticmethod
    def _enforce_boundary(request: AgentRequest, bundle: ContextBundle) -> ContextBundle:
        """Drop anything the run is not allowed to see, and bound the size.

        This is the last line of defence against a future-dated order or a
        memory record written after the replay point leaking into a replay.
        """
        kept: list[ContextEntry] = []
        for entry in bundle.entries:
            if entry.occurred_at is not None and not request.as_of.allows(entry.occurred_at):
                continue
            kept.append(entry)
        total = 0
        bounded: list[ContextEntry] = []
        for entry in kept:
            # Later entries are dropped rather than silently truncated, so a
            # reviewer can tell the context was capped instead of assuming
            # the model simply did not see them.
            size = len(entry.key) + len(str(entry.value))
            if total + size > MAX_CONTEXT_CHARS:
                break
            total += size
            bounded.append(entry)
        return ContextBundle(tuple(bounded))
