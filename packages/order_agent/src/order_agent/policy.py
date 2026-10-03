"""When to record an order automatically, and what to ask when we cannot.

The central rule: **model-reported confidence is never the deciding factor.**
Self-reported 0.95 means nothing until it is calibrated against a labelled set.
What decides is a checklist of things that can be *checked*: is the identity
bound, does the SKU exist in our catalogue, is there an unresolved duplicate, can
the target order be identified uniquely.

Every check here is deterministic. That is the whole point -- the merchant is
not asked to confirm the routine, and is never asked to confirm something we
could have verified ourselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: Ambiguity codes. Stable strings: they end up in review cards and reports.
IDENTITY_UNRESOLVED = "identity_unresolved"
CUSTOMER_NOT_BOUND = "customer_not_bound"
SKU_NOT_IN_CATALOG = "sku_not_in_catalog"
UNIT_MISMATCH = "unit_mismatch"
QUANTITY_MISSING = "quantity_missing"
QUANTITY_INVALID = "quantity_invalid"
EVIDENCE_NOT_FOUND = "evidence_not_found"
DUPLICATE_CANDIDATE_OPEN = "duplicate_candidate_open"
TARGET_ORDER_UNRESOLVED = "target_order_unresolved"
PRICE_RULE_UNKNOWN = "price_rule_unknown"
DELIVERY_RULE_UNKNOWN = "delivery_rule_unknown"
PAYMENT_NOT_RECEIVED = "payment_claim_not_receipt"
EXCEEDS_QUANTITY_LIMIT = "exceeds_quantity_limit"
EXCEEDS_AMOUNT_LIMIT = "exceeds_amount_limit"
PAST_CUTOFF = "change_after_cutoff"
NO_ELIGIBLE_HISTORY = "no_eligible_historical_order"
AMOUNT_UNDETERMINED = "amount_undetermined"

#: Ambiguities that must always stop automatic recording. These are the ones
#: where guessing produces a wrong order, a wrong customer, or a duplicate.
ALWAYS_BLOCKING = frozenset(
    {
        IDENTITY_UNRESOLVED,
        CUSTOMER_NOT_BOUND,
        DUPLICATE_CANDIDATE_OPEN,
        TARGET_ORDER_UNRESOLVED,
        PAYMENT_NOT_RECEIVED,
        PAST_CUTOFF,
    }
)

#: Blocking only when the merchant has not explicitly allowed the loose form.
CONDITIONALLY_BLOCKING = frozenset(
    {SKU_NOT_IN_CATALOG, UNIT_MISMATCH, QUANTITY_MISSING, QUANTITY_INVALID, EVIDENCE_NOT_FOUND, PRICE_RULE_UNKNOWN, AMOUNT_UNDETERMINED, NO_ELIGIBLE_HISTORY}
)

#: Default per-line quantity ceiling. Above this, a human looks.
DEFAULT_MAX_LINE_QUANTITY = 500.0


@dataclass(frozen=True)
class RecordingLimits:
    """Merchant-configurable safety rails."""

    max_line_quantity: float = DEFAULT_MAX_LINE_QUANTITY
    max_order_amount: float | None = None
    allow_price_pending: bool = False
    #: A change arriving after this many hours past the order's cutoff is
    #: handled as an exception rather than an edit.
    change_window_hours: float = 0.0

    def __post_init__(self) -> None:
        if self.max_line_quantity <= 0:
            raise ValueError("max_line_quantity must be positive")
        if self.change_window_hours < 0:
            raise ValueError("change_window_hours must not be negative")


@dataclass(frozen=True)
class RecordingDecision:
    """Why an order was, or was not, recorded without asking a human."""

    record: bool
    reasons: tuple[str, ...] = ()
    blocking: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.record

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record,
            "reasons": list(self.reasons),
            "blocking": list(self.blocking),
            "advisories": list(self.advisories),
        }


def decide_recording(
    *,
    intent: str,
    items: Sequence[Mapping[str, Any]],
    ambiguities: Sequence[str],
    customer_bound: bool,
    catalog_skus: Sequence[str],
    duplicate_candidates_open: bool = False,
    target_resolved: bool = False,
    amount_cents: int | None = None,
    limits: RecordingLimits | None = None,
) -> RecordingDecision:
    """Decide whether this run may be recorded without merchant review.

    ``catalog_skus`` is the merchant's own catalogue. A SKU that is not in it is
    never invented into a priced order -- at best it becomes a review card.
    """
    limits = limits or RecordingLimits()
    reasons: list[str] = []
    blocking: list[str] = []
    advisories: list[str] = []

    if intent in ("inquiry", "follow_up", "chitchat"):
        # Not a failure. An inquiry creates no order and needs no confirmation.
        return RecordingDecision(record=False, reasons=("informational_intent",), advisories=(f"intent:{intent}",))

    if intent == "payment_claim":
        # A customer saying "I paid" is never treated as money received.
        return RecordingDecision(
            record=False,
            reasons=("payment_claim_requires_receipt_check",),
            blocking=(PAYMENT_NOT_RECEIVED,),
        )

    if intent not in ("new_order", "append", "modify", "cancel"):
        return RecordingDecision(record=False, reasons=("intent_not_actionable",), advisories=("intent:unclear",))

    if not customer_bound:
        blocking.append(CUSTOMER_NOT_BOUND)
    if duplicate_candidates_open:
        # An unresolved duplicate must be decided by a person: silently picking
        # one is how the same order gets entered twice.
        blocking.append(DUPLICATE_CANDIDATE_OPEN)
    if intent in ("modify", "cancel", "append") and not target_resolved:
        # A change that cannot be tied to exactly one order is a guess.
        blocking.append(TARGET_ORDER_UNRESOLVED)

    catalog = {str(sku) for sku in catalog_skus}
    priced_total = 0
    for item in items:
        sku = item.get("sku")
        quantity = item.get("quantity")
        unit = item.get("unit")
        if not sku or str(sku) not in catalog:
            blocking.append(SKU_NOT_IN_CATALOG)
            continue
        if quantity is None:
            blocking.append(QUANTITY_MISSING)
            continue
        try:
            numeric = float(quantity)
        except (TypeError, ValueError):
            blocking.append(QUANTITY_INVALID)
            continue
        if numeric <= 0:
            blocking.append(QUANTITY_INVALID)
            continue
        if unit is None:
            blocking.append(UNIT_MISMATCH)
        if numeric > limits.max_line_quantity:
            blocking.append(EXCEEDS_QUANTITY_LIMIT)
        priced_total += numeric

    for code in ambiguities:
        text = str(code)
        if text in ALWAYS_BLOCKING:
            blocking.append(text)
        elif text in CONDITIONALLY_BLOCKING:
            if text == PRICE_RULE_UNKNOWN and limits.allow_price_pending:
                advisories.append(text)
            elif text == AMOUNT_UNDETERMINED and limits.allow_price_pending:
                advisories.append(text)
            else:
                blocking.append(text)

    if amount_cents is not None and limits.max_order_amount is not None:
        if amount_cents > limits.max_order_amount * 100:
            blocking.append(EXCEEDS_AMOUNT_LIMIT)

    blocking = list(dict.fromkeys(blocking))
    if blocking:
        return RecordingDecision(record=False, reasons=("blocked",), blocking=tuple(blocking), advisories=tuple(advisories))
    reasons.append("all_checks_passed")
    if items:
        reasons.append("items_resolved")
    if customer_bound:
        reasons.append("identity_bound")
    return RecordingDecision(record=True, reasons=tuple(reasons), advisories=tuple(advisories))


# ------------------------------------------------------- minimal follow-up


@dataclass(frozen=True)
class FollowUpQuestion:
    """The single question to ask, if one is needed."""

    text: str
    reason: str
    fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "reason": self.reason, "fields": list(self.fields)}


#: Asked only when a necessary field is genuinely missing. Anything else is
#: recorded with the gap noted, because a missing optional field is not worth
#: interrupting a shop for.
NECESSARY_FIELDS: dict[str, str] = {
    "sku": "要哪个商品呢？",
    "quantity": "要几箱？",
    "delivery_date": "什么时候送到？",
}


def build_follow_up(
    ambiguities: Sequence[str],
    *,
    customer_bound: bool,
    resolvable_from_history: Sequence[str] = (),
    missing_information: Sequence[str] = (),
) -> FollowUpQuestion | None:
    """Return at most **one** short question, or ``None``.

    The order of the search follows how a human would actually resolve a
    shorthand order::

        current message and its quote
          -> the current open order
            -> this customer's valid recent orders
              -> rules the merchant has already accepted

    A field that any of those can answer is *not* asked about. Re-asking
    something the customer already told us is the fastest way to make an
    assistant feel stupid.
    """
    if not customer_bound:
        return FollowUpQuestion(text="请问是哪位？", reason=CUSTOMER_NOT_BOUND, fields=("customer",))

    resolvable = {str(item) for item in resolvable_from_history}
    outstanding = [str(code) for code in ambiguities if str(code) not in resolvable]

    # Prefer the product question: a shop can act on "which product" far better
    # than on a re-statement of quantities it may have already heard.
    for code in (SKU_NOT_IN_CATALOG, AMOUNT_UNDETERMINED, PRICE_RULE_UNKNOWN):
        if code in outstanding:
            return FollowUpQuestion(text=NECESSARY_FIELDS["sku"], reason=code, fields=("sku",))

    if TARGET_ORDER_UNRESOLVED in outstanding:
        return FollowUpQuestion(text="要改的是哪一单？", reason=TARGET_ORDER_UNRESOLVED, fields=("target_order",))

    if QUANTITY_MISSING in outstanding or QUANTITY_INVALID in outstanding:
        return FollowUpQuestion(text=NECESSARY_FIELDS["quantity"], reason=QUANTITY_MISSING, fields=("quantity",))

    if NO_ELIGIBLE_HISTORY in outstanding:
        return FollowUpQuestion(
            text="跟哪一单一样？", reason=NO_ELIGIBLE_HISTORY, fields=("target_order",)
        )

    if EXCEEDS_QUANTITY_LIMIT in outstanding:
        return FollowUpQuestion(
            text="数量确认一下，是这么多吗？", reason=EXCEEDS_QUANTITY_LIMIT, fields=("quantity",)
        )

    if DUPLICATE_CANDIDATE_OPEN in outstanding:
        return FollowUpQuestion(
            text="这条和前面那条内容一样，是同一单吗？", reason=DUPLICATE_CANDIDATE_OPEN, fields=("dedupe",)
        )

    # Missing context is the host's problem, not the customer's: return what is
    # absent so the host can supply it and re-run, instead of asking the
    # customer something the system should already know.
    if missing_information:
        return None
    return None
