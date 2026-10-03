"""pyweixin <-> business_agent integration.

This module is the **product integration layer**: it binds the generic core to
the merchant's existing local business layer. It is deliberately thin.

What it adds on top of the core:

* a :class:`~business_agent.contracts.protocols.ContextProvider` that reads the
  merchant's *existing* :class:`~pyweixin.orders.OrderStore` -- the one order
  database, not a second one;
* request construction that reuses the data-minimisation boundary already
  implemented in :mod:`pyweixin.local_parse`;
* a :class:`~pyweixin.integration.order.OrderStoreApplier` that turns a
  proposal into local order state **through the existing rules**, so the cloud
  path and the local model path cannot drift.

What it deliberately does not do: create a second order schema, a second login
flow, a second queue or a second billing system. Those already exist and are
reused.
"""
from __future__ import annotations

from .._packages import ensure_packages_on_path

# Source-checkout convenience only. When the packages are pip-installed this is
# a no-op, and the generic core never imports anything from ``pyweixin``.
ensure_packages_on_path()

from .order import (  # noqa: E402  (paths must exist before the plugins import)
    OrderContextProvider,
    OrderStoreApplier,
    build_order_core,
    build_order_request,
    run_order_parse,
)

__all__ = [
    "OrderContextProvider",
    "OrderStoreApplier",
    "build_order_core",
    "build_order_request",
    "run_order_parse",
]
