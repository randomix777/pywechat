"""order_agent -- the order domain plugin for business_agent.

Everything that is true only of orders lives here: intent vocabulary, output
structure, product matching, auto-recording policy, the minimal follow-up
question, receipt rendering and order memory proposals.

It imports the generic core and nothing else. In particular it does not import
``wechat_ai_assistant``, so it runs on a server with no WeChat automation installed.
"""
from __future__ import annotations

from .contract import (
    INFORMATIONAL_INTENTS,
    INTENTS,
    ORDER_INTENTS,
    OUTPUT_KEYS,
    OUTPUT_SCHEMA,
    validate_output,
)
from .plugin import (
    PROMPT_VERSION,
    SOURCE_CATALOG,
    SOURCE_CUSTOMER,
    SOURCE_FACTS,
    SOURCE_HISTORY,
    SOURCE_ORDER_STATE,
    SYSTEM_PROMPT,
    OrderPlugin,
)
from .policy import (
    ALWAYS_BLOCKING,
    AMOUNT_UNDETERMINED,
    CUSTOMER_NOT_BOUND,
    DUPLICATE_CANDIDATE_OPEN,
    EVIDENCE_NOT_FOUND,
    NO_ELIGIBLE_HISTORY,
    PAYMENT_NOT_RECEIVED,
    PRICE_RULE_UNKNOWN,
    QUANTITY_MISSING,
    SKU_NOT_IN_CATALOG,
    TARGET_ORDER_UNRESOLVED,
    UNIT_MISMATCH,
    FollowUpQuestion,
    RecordingDecision,
    RecordingLimits,
    build_follow_up,
    decide_recording,
)
from .receipt import Receipt, ReceiptItem, format_money, format_quantity, render_receipt

__version__ = "0.1.0"

__all__ = [
    "ALWAYS_BLOCKING",
    "AMOUNT_UNDETERMINED",
    "CUSTOMER_NOT_BOUND",
    "DUPLICATE_CANDIDATE_OPEN",
    "EVIDENCE_NOT_FOUND",
    "FollowUpQuestion",
    "INFORMATIONAL_INTENTS",
    "INTENTS",
    "NO_ELIGIBLE_HISTORY",
    "ORDER_INTENTS",
    "OUTPUT_KEYS",
    "OUTPUT_SCHEMA",
    "OrderPlugin",
    "PAYMENT_NOT_RECEIVED",
    "PRICE_RULE_UNKNOWN",
    "PROMPT_VERSION",
    "QUANTITY_MISSING",
    "Receipt",
    "ReceiptItem",
    "RecordingDecision",
    "RecordingLimits",
    "SKU_NOT_IN_CATALOG",
    "SOURCE_CATALOG",
    "SOURCE_CUSTOMER",
    "SOURCE_FACTS",
    "SOURCE_HISTORY",
    "SOURCE_ORDER_STATE",
    "SYSTEM_PROMPT",
    "TARGET_ORDER_UNRESOLVED",
    "UNIT_MISMATCH",
    "__version__",
    "build_follow_up",
    "decide_recording",
    "format_money",
    "format_quantity",
    "render_receipt",
    "validate_output",
]
