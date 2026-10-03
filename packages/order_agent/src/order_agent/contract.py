"""Order-specific output contract.

Order vocabulary -- intents, item shapes, evidence rules -- lives here, in the
domain plugin, and nowhere in the generic core.

The schema is intentionally compatible in shape with the merchant-facing parse
result the product already exchanges, so the two cannot drift: this module is
the single definition both the local product layer and the cloud gateway use.
"""
from __future__ import annotations

from typing import Any, Mapping

from business_agent.contracts.errors import OutputInvalid
from business_agent.core.validation import (
    enum_field,
    optional_number,
    optional_string,
    require_keys,
    require_string_list,
)

#: Every order-related intent the plugin distinguishes. ``follow_up`` is a
#: customer chasing an existing order; ``payment_claim`` is a customer saying
#: they paid, which is **never** treated as money received.
INTENTS = (
    "new_order",
    "append",
    "modify",
    "cancel",
    "inquiry",
    "follow_up",
    "payment_claim",
    "chitchat",
    "unclear",
)

#: Alias used by the product integration layer, which historically spelled this
#: "allowed intents". Same tuple; kept so the two vocabularies cannot diverge.
ALLOWED_INTENTS = INTENTS

#: Intents that may end in a real order, but only after host-side validation.
ORDER_INTENTS = ("new_order", "append", "modify", "cancel")

#: Intents that are normal, expected outcomes -- not failures. They route to
#: "no order needed" rather than a review card.
INFORMATIONAL_INTENTS = ("inquiry", "follow_up", "chitchat")

MAX_ITEMS = 100
MAX_EVIDENCE = 64

OUTPUT_KEYS = (
    "intent",
    "customer_alias",
    "target_order",
    "items",
    "delivery_date",
    "evidence",
    "missing_fields",
    "ambiguities",
    "suggested_change",
)

#: The JSON schema handed to a model backend that supports structured output.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(OUTPUT_KEYS),
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "customer_alias": {"type": ["string", "null"]},
        "target_order": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sku", "quantity", "unit", "evidence"],
                "properties": {
                    "sku": {"type": ["string", "null"]},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "delivery_date": {"type": ["string", "null"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "suggested_change": {"type": ["object", "null"]},
    },
}


def validate_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Structural validation of an order suggestion.

    Deliberately *only* structural. Whether a SKU exists, whether the quantity
    makes sense and what the order costs are business rules the host applies
    against its own catalogue -- never something the model asserts and the core
    believes.
    """
    require_keys(payload, OUTPUT_KEYS)
    intent = enum_field(payload, "intent", INTENTS)
    optional_string(payload, "customer_alias")
    optional_string(payload, "target_order")
    optional_string(payload, "delivery_date")
    require_string_list(payload, "evidence", MAX_EVIDENCE)
    require_string_list(payload, "missing_fields", MAX_EVIDENCE)
    require_string_list(payload, "ambiguities", MAX_EVIDENCE)

    items = payload.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise OutputInvalid("items must be a bounded list")
    for item in items:
        if not isinstance(item, Mapping):
            raise OutputInvalid("each item must be an object")
        require_keys(item, ("sku", "quantity", "unit", "evidence"))
        optional_string(item, "sku")
        optional_string(item, "unit")
        optional_number(item, "quantity")
        require_string_list(item, "evidence", MAX_EVIDENCE)

    change = payload.get("suggested_change")
    if change is not None and not isinstance(change, Mapping):
        raise OutputInvalid("suggested_change must be an object or null")

    # A payment claim must never be shaped like a settled order; that check is
    # a *contract* rule, not a business judgement, so it belongs here.
    if intent == "payment_claim" and items:
        raise OutputInvalid("a payment claim must not carry order items")
    return dict(payload)
