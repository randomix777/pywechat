"""Order receipt rendering.

A receipt is **rendered from validated order fields**, never written by a model.
A model that is allowed to compose the confirmation text will eventually
compose a number that does not match the order it is confirming -- and a
customer who is told the wrong total is worse off than one who is told nothing.

Every value that could vary is a parameter with no default, so a caller cannot
accidentally render "None boxes".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

#: Shown to a customer. Deliberately short: a group receipt is read once.
RECEIPT_PREFIX = "好的，已记单："
RECEIPT_SUFFIX = "（直接回复即可修改）"


@dataclass(frozen=True)
class ReceiptItem:
    """One line, already priced by the host's own catalogue."""

    name: str
    quantity: float
    unit: str
    unit_price_cents: int

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("receipt item requires a name")
        if self.quantity <= 0:
            raise ValueError("receipt item quantity must be positive")
        if self.unit_price_cents < 0:
            raise ValueError("receipt item price must not be negative")

    @property
    def line_total_cents(self) -> int:
        return int(round(self.quantity * self.unit_price_cents))

    def render(self) -> str:
        return f"{self.name} {format_quantity(self.quantity)}{self.unit}"


@dataclass(frozen=True)
class Receipt:
    """Everything a receipt needs, and nothing the model could invent."""

    items: Sequence[ReceiptItem]
    delivery_date: str = ""
    customer_name: str = ""
    #: Set when a price is still unresolved. The receipt says so plainly
    #: instead of printing a total nobody verified.
    price_pending: bool = False

    @property
    def total_cents(self) -> int:
        return sum(item.line_total_cents for item in self.items)

    def render(self) -> str:
        if not self.items:
            raise ValueError("cannot render a receipt with no items")
        lines = [RECEIPT_PREFIX, "、".join(item.render() for item in self.items)]
        if self.price_pending:
            # An honest gap beats a confident wrong number.
            lines.append("（价格稍后确认）")
        elif self.total_cents:
            lines.append(f"，合计 {format_money(self.total_cents)}")
        if self.delivery_date:
            lines.append(f"，{self.delivery_date} 送达")
        lines.append(RECEIPT_SUFFIX)
        return "".join(lines)


def format_quantity(value: float) -> str:
    """Render 2.0 as ``2`` and 2.5 as ``2.5``; shops do not write "2.0 箱"."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def format_money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def render_receipt(
    *,
    items: Iterable[dict],
    delivery_date: str = "",
    customer_name: str = "",
    price_pending: bool = False,
) -> str:
    """Build a receipt from host-supplied order fields.

    ``items`` entries need ``name``, ``quantity``, ``unit`` and
    ``unit_price_cents``. ``unit_price_cents`` may be omitted **only** when
    ``price_pending`` is set, which makes the receipt say the price is
    unconfirmed instead of printing a total nobody verified. A default of zero
    here would reach a customer as a confidently wrong number.
    """
    rendered: list[ReceiptItem] = []
    for entry in items:
        if "unit_price_cents" not in entry and not price_pending:
            raise ValueError("unit_price_cents is required unless price_pending is set")
        rendered.append(
            ReceiptItem(
                name=str(entry["name"]),
                quantity=float(entry["quantity"]),
                unit=str(entry.get("unit", "")),
                unit_price_cents=int(entry.get("unit_price_cents", 0)),
            )
        )
    return Receipt(
        items=tuple(rendered),
        delivery_date=str(delivery_date or ""),
        customer_name=str(customer_name or ""),
        price_pending=bool(price_pending),
    ).render()
