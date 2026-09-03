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


def test_rag_supplier_margin_and_default_margin_are_source_aware():
    """Mapped RAG suppliers use their margin and unmapped suppliers use default."""
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
                sku="RAG-DEFAULT",
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

    assert [line.base_ars for line in order.lines] == [Decimal("125.00"), Decimal("120.00")]


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


def test_non_ars_conversion_happens_before_margin_and_totals():
    """USD offer prices convert to ARS before supplier margin and totals."""
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

    assert order.lines[0].base_ars == Decimal("12500.00")
    assert order.lines[0].final_ars == Decimal("11250.00")
    assert order.subtotal == Decimal("37500.00")
    assert order.total == Decimal("33750.00")
