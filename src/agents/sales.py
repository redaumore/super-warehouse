"""Sales agent: quotation and per-line adjustments.

Turns already-resolved order items plus the customer's commercial condition
(list + particular discounts) into a quote, delegating every price to the pure
pricing engine (``src.pricing.engine``) so no agent ever re-derives a price.
Also applies the owner's per-line adjustments — e.g. "hacé un 5% de descuento
extra en clavos" — by re-pricing only the affected lines and recording the
absolute discount in ``adjustment`` (the ``order_items.adjustment`` column).

This module performs no I/O: quoting is a pure computation over resolved items,
which keeps the agent testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from src.pricing.engine import compute_final

_CENT = Decimal("0.01")


class AdjustmentTargetError(Exception):
    """Raised when an adjustment names a line that is not in the quote."""


@dataclass(frozen=True)
class ItemInput:
    """A resolved order line waiting to be priced (no price math inside)."""

    sku: str
    cantidad: int
    base_price: Decimal | float | int
    description: str | None = None


@dataclass(frozen=True)
class QuoteLine:
    """One priced line of a quote."""

    sku: str
    cantidad: int
    base_price: Decimal
    final_price: Decimal
    adjustment: Decimal = Decimal("0")  # absolute discount amount applied
    description: str | None = None

    @property
    def line_total(self) -> Decimal:
        return (self.final_price * self.cantidad).quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    """A full quote: priced lines and their total."""

    lines: tuple[QuoteLine, ...]
    currency: str = "ARS"

    @property
    def total(self) -> Decimal:
        return sum((line.line_total for line in self.lines), Decimal(0)).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )

    def line_for(self, sku: str) -> QuoteLine:
        for line in self.lines:
            if line.sku == sku:
                return line
        raise KeyError(f"sku not in quote: {sku}")


def quote_order(
    items: Iterable[ItemInput],
    list_discount: Decimal | float | int | None,
    particular_discount: Decimal | float | int | None,
) -> Quote:
    """Price every line with the customer's discounts and build the quote.

    Each line's final price is ``base × (1 − list) × (1 − particular)`` via the
    pricing engine — discounts compound, never sum.
    """
    lines = tuple(
        QuoteLine(
            sku=item.sku,
            cantidad=item.cantidad,
            base_price=Decimal(str(item.base_price)).quantize(_CENT, rounding=ROUND_HALF_UP),
            final_price=compute_final(item.base_price, list_discount, particular_discount),
            description=item.description,
        )
        for item in items
    )
    return Quote(lines=lines)


def adjust_line(quote: Quote, sku: str, extra_discount_pct: Decimal | float | int) -> Quote:
    """Return a new quote with one line re-priced by an extra discount.

    ``extra_discount_pct`` is applied multiplicatively to that line's final
    price (0.05 = 5% off), and the absolute discount given is recorded in the
    line's ``adjustment``. Other lines are untouched.
    """
    pct = Decimal(str(extra_discount_pct))
    updated = []
    found = False
    for line in quote.lines:
        if line.sku != sku:
            updated.append(line)
            continue
        found = True
        new_final = (line.final_price * (Decimal(1) - pct)).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        updated.append(
            QuoteLine(
                sku=line.sku,
                cantidad=line.cantidad,
                base_price=line.base_price,
                final_price=new_final,
                adjustment=(line.final_price - new_final).quantize(_CENT, rounding=ROUND_HALF_UP),
                description=line.description,
            )
        )
    if not found:
        raise KeyError(f"sku not in quote: {sku}")
    return Quote(lines=tuple(updated), currency=quote.currency)


def apply_adjustments(
    quote: Quote, adjustments: Iterable[tuple[str, Decimal | float | int]]
) -> Quote:
    """Apply several ``(sku, extra_discount_pct)`` adjustments in one pass."""
    result = quote
    for sku, pct in adjustments:
        try:
            result = adjust_line(result, sku, pct)
        except KeyError as exc:
            raise AdjustmentTargetError(f"adjustment target not in quote: {sku}") from exc
    return result