"""Order domain wiring for the merchant's local product.

Three small adapters, and nothing else:

* :class:`OrderContextProvider` reads the merchant's existing ``OrderStore``
  and returns it as a :class:`~business_agent.contracts.request.ContextBundle`,
  bounded by the run's knowledge cut-off.
* :func:`build_order_request` turns one local message into a core request,
  reusing the fields :mod:`wechat_ai_assistant.local_parse` already decided to send.
* :class:`OrderStoreApplier` writes a validated proposal back **through
  :meth:`~wechat_ai_assistant.orders.OrderStore.apply_cloud_suggestion`**, the same entry
  point the gateway path already uses. There is exactly one place where a
  suggestion becomes an order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from business_agent.contracts.protocols import ContextProvider
from business_agent.contracts.request import (
    AgentRequest,
    AsOf,
    Budget,
    ContextBundle,
    ContextEntry,
    Event,
    Policy,
    Scope,
)
from business_agent.contracts.result import AgentResult, RunStatus
from order_agent import OrderPlugin, RecordingLimits
from order_agent.plugin import (
    SOURCE_CATALOG,
    SOURCE_CUSTOMER,
    SOURCE_FACTS,
    SOURCE_HISTORY,
    SOURCE_ORDER_STATE,
)

#: The agent task type the product registers with the core.
TASK_ORDER_PARSE = "order_parse"

#: How far back a message's context reaches. Matches the existing
#: ``LocalParseService`` default so the two paths see the same window.
DEFAULT_LOOKBACK_SECONDS = 90.0


class OrderContextProvider(ContextProvider):
    """Serves order context out of the merchant's existing local database.

    Two boundaries are enforced here rather than trusted to the caller:

    * **tenant scope** -- every query filters on ``merchant_id``; there is no code
      path in which a request can name a different merchant.
    * **time** -- history, facts and orders are filtered by ``sent_at_epoch <=
      as_of``. A replay must never see the answer it is being asked to produce.
    """

    def __init__(self, orders: Any, tenant: str, group: str = "default", recent_message_limit: int = 20,
                 fact_limit: int = 20, order_limit: int = 10):
        self.orders = orders
        self.tenant = tenant
        self.group = group
        self.recent_message_limit = int(recent_message_limit)
        self.fact_limit = int(fact_limit)
        self.order_limit = int(order_limit)

    def collect(self, request: AgentRequest) -> ContextBundle:
        event = request.primary_event
        message = self._message(request, event)
        if message is None:
            return ContextBundle()
        as_of_text = event.occurred_at.isoformat()
        entries: list[ContextEntry] = []
        entries.extend(self._customer_entries(request, message))
        entries.extend(self._catalog_entries(request))
        entries.extend(self._recent_messages(request, message, as_of_text))
        entries.extend(self._fact_entries(request, message, as_of_text))
        entries.extend(self._history_entries(request, message))
        entries.extend(self._order_state_entries(request, message))
        return ContextBundle(tuple(entries))

    def describe_missing(self, request: AgentRequest) -> Sequence[str]:
        """Say plainly what the local database cannot answer.

        This is what the core turns into a "needs more context" request, which
        the merchant's product resolves locally. It never becomes a question
        asked of a customer.
        """
        event = request.primary_event
        message = self._message(request, event)
        missing: list[str] = []
        if message is None:
            missing.append("本地订单库中没有这条消息，请先完成历史读取")
        elif not message["customer_id"]:
            missing.append("这条消息的发送人还没有绑定到客户档案")
        elif message["sent_at"] is None and message["sent_at_epoch"] is None:
            missing.append("这条消息没有可用的发送时间，无法确定历史截止点")
        # Query the catalogue directly: ``request.context`` is still empty at
        # this point, because this method runs *instead of* ``collect``.
        has_products = self.orders.db.execute(
            "SELECT 1 FROM products WHERE merchant_id=? LIMIT 1", (self.tenant,)
        ).fetchone()
        if not has_products:
            missing.append("商家还没有录入商品资料")
        return missing

    # ------------------------------------------------------------- internals

    def _message(self, request: AgentRequest, event: Event):
        message_id = str(request.attributes.get("message_id") or event.event_id)
        return self.orders.db.execute(
            "SELECT * FROM messages WHERE merchant_id=? AND id=?", (self.tenant, message_id)
        ).fetchone()

    def _customer_entries(self, request: AgentRequest, message) -> list[ContextEntry]:
        if not message["customer_id"]:
            return []
        row = self.orders.db.execute(
            "SELECT id,name,address FROM customers WHERE merchant_id=? AND id=?",
            (self.tenant, message["customer_id"]),
        ).fetchone()
        if row is None:
            return []
        return [
            ContextEntry(
                key="customer",
                value={"id": row["id"], "name": row["name"], "address": row["address"] or ""},
                source=SOURCE_CUSTOMER,
                confirmed=True,
            )
        ]

    def _catalog_entries(self, request: AgentRequest) -> list[ContextEntry]:
        rows = self.orders.db.execute(
            "SELECT p.sku,p.name,p.unit,group_concat(pa.alias,'|') aliases FROM products p"
            " LEFT JOIN product_aliases pa ON pa.product_id=p.id WHERE p.merchant_id=? GROUP BY p.id"
            " ORDER BY p.name",
            (self.tenant,),
        ).fetchall()
        return [
            ContextEntry(
                key=f"product:{row['sku']}",
                value={
                    "sku": row["sku"],
                    "name": row["name"],
                    "unit": row["unit"],
                    "aliases": [alias for alias in (row["aliases"] or "").split("|") if alias],
                },
                source=SOURCE_CATALOG,
                confirmed=True,
            )
            for row in rows
        ]

    def _recent_messages(self, request: AgentRequest, message, as_of_text: str) -> list[ContextEntry]:
        if message["sent_at_epoch"] is None:
            return []
        rows = self.orders.db.execute(
            "SELECT sender,content,sent_at FROM messages WHERE merchant_id=? AND group_id=? AND customer_id=?"
            " AND sent_at_epoch<=? AND sent_at_epoch>? ORDER BY sent_at_epoch DESC LIMIT ?",
            (
                self.tenant,
                self.group,
                message["customer_id"],
                message["sent_at_epoch"],
                message["sent_at_epoch"] - DEFAULT_LOOKBACK_SECONDS,
                self.recent_message_limit,
            ),
        ).fetchall()
        return [
            ContextEntry(
                key=f"recent:{index}",
                value={"sender": row["sender"] or "", "content": row["content"], "sent_at": row["sent_at"]},
                source=SOURCE_HISTORY,
            )
            for index, row in enumerate(reversed(rows))
        ]

    def _fact_entries(self, request: AgentRequest, message, as_of_text: str) -> list[ContextEntry]:
        # Only human-confirmed facts, and only those already effective at the
        # message time. A model must never see a habit that a later order
        # created -- that is how a replay "predicts" its own answer.
        rows = self.orders.db.execute(
            "SELECT id,kind,value FROM memory_facts WHERE merchant_id=? AND customer_id=? AND confirmed=1"
            " AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)"
            " ORDER BY effective_from DESC LIMIT ?",
            (self.tenant, message["customer_id"], as_of_text, as_of_text, self.fact_limit),
        ).fetchall()
        return [
            ContextEntry(key=f"fact:{row['id']}", value={"kind": row["kind"], "value": row["value"]}, source=SOURCE_FACTS, confirmed=True)
            for row in rows
        ]

    def _history_entries(self, request: AgentRequest, message) -> list[ContextEntry]:
        if message["sent_at_epoch"] is None:
            return []
        rows = self.orders.db.execute(
            "SELECT id,status,delivery_date FROM orders WHERE merchant_id=? AND customer_id=? AND status='confirmed'"
            " AND business_at_epoch<=? ORDER BY business_at_epoch DESC LIMIT ?",
            (self.tenant, message["customer_id"], message["sent_at_epoch"], self.order_limit),
        ).fetchall()
        entries: list[ContextEntry] = []
        for row in rows:
            detail = self.orders.order_detail(self.tenant, row["id"])
            entries.append(
                ContextEntry(
                    key=f"order:{row['id']}",
                    value={
                        "order_id": row["id"],
                        "status": row["status"],
                        "delivery_date": row["delivery_date"] or "",
                        "items": [
                            {"sku": item["sku"], "quantity": item["quantity"], "unit": item["unit"]}
                            for item in detail["items"]
                        ],
                    },
                    source=SOURCE_HISTORY,
                    occurred_at=_epoch_to_datetime(row.get("business_at_epoch") if hasattr(row, "keys") else None),
                    confirmed=True,
                )
            )
        return entries

    def _order_state_entries(self, request: AgentRequest, message) -> list[ContextEntry]:
        rows = self.orders.db.execute(
            "SELECT id,status FROM orders WHERE merchant_id=? AND customer_id=? AND status IN ('draft','review')"
            " ORDER BY created_at DESC LIMIT ?",
            (self.tenant, message["customer_id"], self.order_limit),
        ).fetchall()
        return [
            ContextEntry(
                key=f"open_order:{row['id']}",
                value={"order_id": row["id"], "status": row["status"]},
                source=SOURCE_ORDER_STATE,
            )
            for row in rows
        ]

    def _catalog(self, request: AgentRequest) -> list[str]:
        return [str(entry.value.get("sku")) for entry in request.context.by_source(SOURCE_CATALOG) if isinstance(entry.value, Mapping)]

    def _has_customer(self, request: AgentRequest) -> bool:
        return bool(request.context.by_source(SOURCE_CUSTOMER))

    def _target_resolved(self, output: Mapping[str, Any], request: AgentRequest) -> bool:
        target = output.get("target_order")
        if not target:
            return False
        known = set(request.attributes.get("known_order_ids", ()))
        return not known or str(target) in known


def _epoch_to_datetime(value: Any) -> datetime | None:
    try:
        if value is None:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def build_order_request(
    message: Mapping[str, Any],
    *,
    tenant: str,
    group: str = "default",
    request_id: str = "",
    idempotency_key: str = "",
    policy: Policy | None = None,
    budget: Budget | None = None,
    limits: RecordingLimits | None = None,
    duplicate_candidates_open: bool = False,
    known_order_ids: Sequence[str] = (),
) -> AgentRequest:
    """Build a core request from one row of the merchant's message table.

    ``message`` is a mapping with ``id``, ``content``, ``kind``, ``sender``,
    ``sent_at`` and ``observed_at``. The knowledge cut-off is the message's own
    send time, which is what makes replay honest.
    """
    sent_at = message.get("sent_at") or message.get("observed_at")
    moment = _parse_moment(sent_at)
    request_id = request_id or f"order-{message.get('id', '')}"
    return AgentRequest(
        request_id=request_id,
        idempotency_key=idempotency_key or f"msg-{message.get('id', '')}",
        task_type=TASK_ORDER_PARSE,
        scope=Scope(tenant_id=tenant, conversation_id=group),
        events=(
            Event(
                event_id=str(message.get("id", "")),
                body=str(message.get("content", "")),
                occurred_at=moment,
                actor=str(message.get("sender", "") or ""),
                channel=group,
                kind=str(message.get("kind", "text")),
            ),
        ),
        as_of=AsOf(instant=moment, lookback=_timedelta(DEFAULT_LOOKBACK_SECONDS)),
        policy=policy or Policy(name="order-default"),
        budget=budget or Budget(timeout_seconds=60.0),
        attributes={
            "message_id": str(message.get("id", "")),
            "group": group,
            "duplicate_candidates_open": bool(duplicate_candidates_open),
            "known_order_ids": tuple(known_order_ids),
        },
    )


def _parse_moment(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _timedelta(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


class OrderStoreApplier:
    """Applies a validated proposal to the merchant's existing order store.

    Delegates to :meth:`~wechat_ai_assistant.orders.OrderStore.apply_cloud_suggestion`,
    which is the same code path the existing gateway integration uses. The
    generic core never writes an order; this class is the product's side of that
    boundary, and it goes through the product's own rules.
    """

    def __init__(self, orders: Any, tenant: str):
        self.orders = orders
        self.tenant = tenant

    def apply(self, result: AgentResult, group: str, job_id: str = "") -> Mapping[str, Any]:
        if not result.ok:
            return {"outcome": "skipped", "reason": result.status, "order_id": None}
        suggestion = _to_order_suggestion(result.output)
        message_id = str(result.output.get("_message_id", ""))
        if not message_id:
            return {"outcome": "rejected", "reason": "no message id in result", "order_id": None}
        return self.orders.apply_cloud_suggestion(self.tenant, group, message_id, suggestion, job_id=job_id)


def _to_order_suggestion(output: Mapping[str, Any]) -> dict[str, Any]:
    """Map a core result back onto the product's existing suggestion shape.

    The output keys already match; this strips the integration-only fields the
    host attached, so the local validator sees exactly the documented shape.
    """
    return {
        "intent": output.get("intent", "unclear"),
        "customer_alias": output.get("customer_alias"),
        "target_order": output.get("target_order"),
        "items": output.get("items", []),
        "delivery_date": output.get("delivery_date"),
        "evidence": output.get("evidence", []),
        "missing_fields": output.get("missing_fields", []),
        "ambiguities": output.get("ambiguities", []),
        "suggested_change": output.get("suggested_change"),
    }


def build_order_core(backend: Any, orders: Any, tenant: str, group: str = "default", limits: RecordingLimits | None = None) -> Any:
    """Assemble the core for the order domain against the merchant's store."""
    from business_agent import AgentCore

    return AgentCore(
        backend=backend,
        plugin=OrderPlugin(limits=limits),
        context_providers=(OrderContextProvider(orders, tenant, group),),
    )


def run_order_parse(
    backend: Any,
    orders: Any,
    tenant: str,
    group: str,
    message: Mapping[str, Any],
    limits: RecordingLimits | None = None,
) -> AgentResult:
    """One-shot helper: build, run and return a result. Convenience only."""
    core = build_order_core(backend, orders, tenant, group, limits)
    request = build_order_request(message, tenant=tenant, group=group)
    return core.run(request)
