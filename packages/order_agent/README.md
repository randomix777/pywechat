# order_agent

The order domain plugin for [`business_agent`](../business_agent).

Everything that is true only of orders lives here: intent vocabulary, output
structure, auto-recording policy, the minimal follow-up question, receipt
rendering and order memory proposals.

It imports the generic core and nothing else — no WeChat, no Windows
automation, no product database. It runs on a server with none of those
installed.

## Auto-recording

The model proposes; deterministic code decides. Model-reported confidence is
never the deciding factor.

An order is recorded without asking a human only when **all** of these hold:

- identity is bound (by the host, not by the model naming someone)
- every SKU is in the merchant's catalogue
- quantity is present, positive and under the limit
- unit matches the catalogue
- no unresolved duplicate-message candidate exists
- a change can be tied to exactly one order
- every cited quote appears verbatim in the input
- no amount, quantity or fulfilment exception fired

```python
from order_agent import decide_recording, RecordingLimits

decision = decide_recording(
    intent="new_order",
    items=[{"sku": "APL", "quantity": 2, "unit": "箱"}],
    ambiguities=[],
    customer_bound=True,
    catalog_skus=["APL", "EGG"],
    limits=RecordingLimits(max_line_quantity=100, max_order_amount=5000),
)
decision.record      # bool
decision.blocking    # the codes that stopped it
```

## Minimal follow-up

`build_follow_up()` returns **at most one** question, or `None`. It searches in
the order a human would use:

```
current message and its quote → current order → this customer's valid history → accepted defaults
```

A field any of those can answer is not asked about. Missing *system* context is
never asked of the customer; it is returned to the host instead.

## Receipts

Receipts are rendered from validated order fields. A model never writes the
confirmation text — it will eventually write a number that does not match the
order, and a customer told the wrong total is worse off than one told nothing.

```python
from order_agent import render_receipt

render_receipt(
    items=[{"name": "苹果", "quantity": 2, "unit": "箱", "unit_price_cents": 2600}],
    delivery_date="2026-09-26",
)
# '好的，已记单：苹果 2箱，合计 52.00，2026-09-26 送达（直接回复即可修改）'
```

A missing `unit_price_cents` raises rather than defaulting to zero, unless
`price_pending=True` — in which case the receipt says the price is unconfirmed
instead of printing an unverified total.
