"""Pure pricing functions for the ferretería MVP.

Fixed by the pricing-engine spec: the base price is the supplier cost marked up
by the applied margin, and the final price applies the customer's list and
particular discounts multiplicatively — list discount first, then particular.
The two are NEVER added together.

This module performs no I/O: it is the single source of truth for price
computation so agents never re-derive prices ad hoc. All results are quantized
to 2 decimals with `ROUND_HALF_UP`, matching the `Numeric(12, 2)` columns in the
catalog and order tables.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")

PriceInput = Decimal | float | int
DiscountInput = Decimal | float | int | None


def _as_decimal(value: PriceInput | None) -> Decimal:
    """Coerce a price/discount input to Decimal; absent values are zero.

    Floats are converted through their string representation so `0.35` becomes
    `Decimal("0.35")` instead of the binary-float expansion.
    """
    if value is None:
        return Decimal(0)
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def compute_base(cost: PriceInput, margin: DiscountInput = None) -> Decimal:
    """Compute the base price: ``cost × (1 + margin)``.

    An absent (``None``) or zero margin leaves the base price equal to the cost.
    """
    return (_as_decimal(cost) * (Decimal(1) + _as_decimal(margin))).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )


def compute_final(
    base: PriceInput,
    list_discount: DiscountInput,
    particular_discount: DiscountInput,
) -> Decimal:
    """Compute the final price: ``base × (1 − list_discount) × (1 − particular_discount)``.

    Discounts compound multiplicatively — list first, then particular — and are
    never summed. Absent discounts (``None``) are treated as zero percent.
    """
    final = _as_decimal(base)
    final *= Decimal(1) - _as_decimal(list_discount)
    final *= Decimal(1) - _as_decimal(particular_discount)
    return final.quantize(_CENT, rounding=ROUND_HALF_UP)
