"""Unit tests for source-aware customer-order pricing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.pricing.order_pricing import MissingRateError, PricingLine, compute_order


def test_local_lines_use_cost_and_applied_margin_not_list_price():
    """A local line derives its base from cost and the applied catalog margin."""
    order = compute_order(
        [
            PricingLine(
                sku="LOCAL-1",
                cantidad=2,
                source="LOCAL",
                name="Local item",
                cost=Decimal("100.00"),
                margin=Decimal("0.35"),
            )
        ],
        list_discount=Decimal("0.10"),
    )

    assert order.lines[0].base_ars == Decimal("135.00")
    assert order.lines[0].final_ars == Decimal("121.50")
    assert order.subtotal == Decimal("270.00")
    assert order.total == Decimal("243.00")


def test_rag_lines_never_apply_supplier_or_default_margin():
    """RAG lines price at the converted offer: no supplier margin, no default."""
    order = compute_order(
        [
            PricingLine(
                sku="RAG-MAPPED",
                cantidad=1,
                source="RAG",
                price=Decimal("100.00"),
                currency="ARS",
                codigo_proveedor="SUP",
                supplier="Supplier",
            ),
            PricingLine(
                sku="RAG-UNMAPPED",
                cantidad=1,
                source="RAG",
                price=Decimal("100.00"),
                currency="ARS",
                codigo_proveedor="UNKNOWN",
            ),
        ],
        supplier_margin={"SUP": Decimal("0.25")},
        default_margin=Decimal("0.20"),
    )

    # The owner decision: the offer price is the base AND the sale reference;
    # margins only apply to LOCAL lines.
    assert [line.base_ars for line in order.lines] == [Decimal("100.00"), Decimal("100.00")]
    assert [line.final_ars for line in order.lines] == [Decimal("100.00"), Decimal("100.00")]


def test_missing_non_ars_rate_raises():
    """A non-ARS line cannot be priced without its exchange rate."""
    with pytest.raises(MissingRateError, match="USD"):
        compute_order(
            [
                PricingLine(
                    sku="RAG-USD",
                    cantidad=1,
                    source="RAG",
                    price=Decimal("10.00"),
                    currency="USD",
                )
            ],
            rate=lambda _currency: None,
        )


def test_non_ars_conversion_applies_before_discounts_and_totals():
    """USD offer prices convert to ARS as the base; only discounts apply after."""
    order = compute_order(
        [
            PricingLine(
                sku="RAG-USD",
                cantidad=3,
                source="RAG",
                price=Decimal("10.00"),
                currency="USD",
                codigo_proveedor="SUP",
            )
        ],
        rate={"USD": Decimal("1000.00")},
        supplier_margin={"SUP": Decimal("0.25")},
        list_discount=Decimal("0.10"),
    )

    assert order.lines[0].base_ars == Decimal("10000.00")
    assert order.lines[0].final_ars == Decimal("9000.00")
    assert order.subtotal == Decimal("30000.00")
    assert order.total == Decimal("27000.00")


def test_line_subtotal_extends_unit_price_by_quantity():
    """The shared helper quantizes unit × quantity HALF_UP to the cent."""
    from src.pricing.order_pricing import line_subtotal

    assert line_subtotal(Decimal("2448.00"), 2) == Decimal("4896.00")
    assert line_subtotal(Decimal("2937.605"), 1) == Decimal("2937.61")
