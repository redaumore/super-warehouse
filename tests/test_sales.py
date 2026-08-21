"""Sales agent tests (task 2.6).

Quoting is a pure computation over resolved items — no DB, no network. Verifies
the pricing-engine contract is honored (discounts compound, never sum), totals
accumulate per line, and per-line adjustments re-price only the affected line.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.agents.sales import (
    AdjustmentTargetError,
    ItemInput,
    Quote,
    QuoteLine,
    adjust_line,
    apply_adjustments,
    quote_order,
)

_CLAVOS = ItemInput(
    sku="CLV-001",
    cantidad=10,
    base_price=Decimal("100.00"),
    description="Clavos Paris 2 Pulgadas (50mm)",
)
_TORNILLOS = ItemInput(
    sku="TRN-002", cantidad=5, base_price=Decimal("50.00"), description="Tornillos M6 x 30"
)


def _two_line_quote() -> Quote:
    return quote_order((_CLAVOS, _TORNILLOS), None, None)


def test_quote_applies_compound_discounts():
    """Final = base × (1 − list) × (1 − particular); 100 × 0.8 × 0.9 = 72, never 70."""
    quote = quote_order((_CLAVOS,), Decimal("0.20"), Decimal("0.10"))
    assert quote.lines[0].final_price == Decimal("72.00")
    assert quote.lines[0].final_price != Decimal("70.00")


def test_quote_without_discounts_prices_at_base():
    quote = quote_order((_CLAVOS,), None, None)
    assert quote.lines[0].final_price == Decimal("100.00")
    assert quote.lines[0].adjustment == Decimal("0")


def test_quote_total_accumulates_line_totals():
    """10 × 100 + 5 × 50 = 1250; quantity multiplies the unit price."""
    quote = _two_line_quote()
    assert quote.lines[0].line_total == Decimal("1000.00")
    assert quote.lines[1].line_total == Decimal("250.00")
    assert quote.total == Decimal("1250.00")


def test_quote_rounds_half_up_to_cents():
    quote = quote_order((ItemInput(sku="X", cantidad=1, base_price=Decimal("12.345")),), None, None)
    assert quote.lines[0].final_price == Decimal("12.35")


def test_adjust_line_applies_extra_discount_to_one_line_only():
    quote = adjust_line(_two_line_quote(), "CLV-001", Decimal("0.05"))
    assert quote.lines[0].final_price == Decimal("95.00")  # 100 × 0.95
    assert quote.lines[0].adjustment == Decimal("5.00")
    assert quote.lines[1].final_price == Decimal("50.00")  # untouched
    assert quote.lines[1].adjustment == Decimal("0")


def test_adjust_line_unknown_sku_raises():
    with pytest.raises(KeyError, match="not in quote"):
        adjust_line(_two_line_quote(), "NOPE-9", Decimal("0.05"))


def test_adjust_line_keeps_original_quote_immutable():
    original = _two_line_quote()
    adjusted = adjust_line(original, "CLV-001", Decimal("0.05"))
    assert original.lines[0].final_price == Decimal("100.00")
    assert adjusted.lines[0].final_price == Decimal("95.00")


def test_apply_adjustments_multi_line():
    quote = apply_adjustments(
        _two_line_quote(),
        [("CLV-001", Decimal("0.10")), ("TRN-002", Decimal("0.05"))],
    )
    assert quote.lines[0].final_price == Decimal("90.00")
    assert quote.lines[1].final_price == Decimal("47.50")


def test_apply_adjustments_unknown_target_raises():
    with pytest.raises(AdjustmentTargetError, match="not in quote"):
        apply_adjustments(_two_line_quote(), [("NOPE-9", Decimal("0.05"))])


def test_quote_line_for_unknown_sku_raises():
    quote = _two_line_quote()
    with pytest.raises(KeyError):
        quote.line_for("NOPE-9")