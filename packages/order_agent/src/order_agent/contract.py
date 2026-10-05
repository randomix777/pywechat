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

#: ``items`` 每一项的字段清单。**唯一定义** ——
#: 校验、JSON Schema、提示词、以及产品层（``orders.py``）都从这里取。
#:
#: 以前产品层自己硬编码了一份，于是契约一改它就静默地把全部模型输出拒掉，
#: 报错还只说「不符合要求的结构」，没人看得出是哪一边过时了。
ITEM_KEYS = ("said", "brand", "category", "sku", "quantity", "unit", "evidence")

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
                "required": list(ITEM_KEYS),
                "properties": {
                    # **客户原话里对商品的描述**，必填。
                    #
                    # 群里客户下单很随便，不会报 SKU，通常只说「某牌子的酱油」
                    # 「上次那种」「那种大瓶的」。这句话是唯一**确定来自客户**的
                    # 商品信息，丢不得 —— 匹配不上时商户还要靠它人工辨认。
                    "said": {"type": "string"},
                    # 从 said 里拆出的品牌与品类，可空。拆出来是为了能拿去和
                    # 商品目录的别名做匹配；拆不出来就留空，不要硬编。
                    "brand": {"type": ["string", "null"]},
                    "category": {"type": ["string", "null"]},
                    # SKU **由宿主按商户目录匹配后填入，模型一律留 null**。
                    #
                    # 模型猜错 SKU 的代价比留空大得多：猜错了商户会照着发错货，
                    # 而留空至少草稿还在、商户能在确认卡上补。
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
        require_keys(item, ITEM_KEYS)
        # ``said`` 是客户原话，必须是非空字符串 —— 没有它，匹配不上时
        # 商户就完全看不出客户要的是什么，草稿等于作废。
        if not isinstance(item.get("said"), str) or not item["said"].strip():
            raise OutputInvalid("said must record what the customer actually wrote")
        optional_string(item, "brand")
        optional_string(item, "category")
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
