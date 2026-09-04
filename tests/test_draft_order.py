"""Integration tests for persistence of source-aware customer draft orders."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.agents.customer import _supplier_margin_source
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    OrderItem,
    StockReservation,
    Supplier,
)
from src.pricing.order_pricing import PricedLine, PricedOrder, PricingLine, compute_order
from src.sourcing.draft_order import persist_draft_order


def _postgres_up() -> bool:
    try:
        engine = create_engine(
            get_settings().sqlalchemy_database_url, connect_args={"connect_timeout": 2}
        )
        with engine.connect():
            pass
        engine.dispose()
        return True
    except (OperationalError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _postgres_up(), reason="Postgres not running (make db-up)")


@pytest.fixture(autouse=True)
def _clean_schema(clean_schema):
    yield


@pytest.fixture
def customer_ctx(db_session):
    """Seed one customer, one local product, and canonical inventory."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial="Customer One",
            telefono_norm="+5491155551234",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(
        Supplier(id=1, code="SUP", business_name="Supplier", default_margin_pct=Decimal(0))
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="LOCAL-1",
            supplier_id=1,
            nombre_oficial="Local item",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=[],
        )
    )
    db_session.add(Inventory(sku_id="LOCAL-1", quantity_on_hand=10))
    db_session.flush()
    return db_session, db_session.get(Cliente, 1)


def test_persist_draft_order_reserves_local_and_keeps_rag_snapshot(customer_ctx):
    """Local lines reserve stock while RAG lines remain catalog-independent snapshots."""
    session, customer = customer_ctx
    priced = PricedOrder(
        lines=(
            PricedLine(
                sku="LOCAL-1",
                cantidad=2,
                base_ars=Decimal("135.00"),
                final_ars=Decimal("135.00"),
                moneda="ARS",
                source="LOCAL",
                name="Local item",
                precio_original=Decimal("100.00"),
            ),
            PricedLine(
                sku="AMX-AT-5044",
                cantidad=3,
                base_ars=Decimal("162.60"),
                final_ars=Decimal("162.60"),
                moneda="ARS",
                source="RAG",
                name="RAG item",
                supplier="AMX",
                precio_original=Decimal("135.50"),
                codigo_proveedor="AMX",
            ),
        ),
        subtotal=Decimal("757.80"),
        total=Decimal("757.80"),
    )

    order = persist_draft_order(session, customer, priced)

    assert order.subtotal == Decimal("757.80")
    assert order.total == Decimal("757.80")
    reservations = session.scalars(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    ).all()
    assert [(row.sku, row.cantidad) for row in reservations] == [("LOCAL-1", 2)]
    items = session.scalars(select(OrderItem).where(OrderItem.order_id == order.order_id)).all()
    assert items[1].sku == "AMX-AT-5044"
    assert items[1].source == "RAG"
    assert items[1].name == "RAG item"
    assert items[1].precio_original == Decimal("135.5000")


def test_supplier_margin_edit_keeps_persisted_order_lines_frozen(customer_ctx):
    """Editing a supplier margin afterwards never re-prices persisted order lines."""
    session, customer = customer_ctx
    supplier = session.get(Supplier, 1)
    supplier.default_margin_pct = Decimal("0.25")
    session.flush()

    priced = compute_order(
        (
            PricingLine(
                sku="RAG-SUP-1",
                cantidad=1,
                source="RAG",
                name="RAG item",
                price=Decimal("100.00"),
                currency="ARS",
                supplier="SUP",
                codigo_proveedor="SUP",
            ),
        ),
        supplier_margin=_supplier_margin_source(session),
        default_margin=Decimal("0.20"),
    )
    assert priced.lines[0].base_ars == Decimal("125.00")  # 100 × 1.25 at persist time

    order = persist_draft_order(session, customer, priced)
    session.commit()

    # Editing the supplier margin after persistence must not touch the snapshot.
    supplier.default_margin_pct = Decimal("0.50")
    session.commit()

    session.refresh(order)
    item = session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("125.00")
    assert item.final_price == Decimal("125.00")
    assert order.subtotal == Decimal("125.00")
    assert order.total == Decimal("125.00")


def test_persist_draft_order_keeps_totals_null_when_conversion_is_pending(customer_ctx):
    """Pending conversion stores snapshots and leaves order totals unset."""
    session, customer = customer_ctx
    priced = PricedOrder(
        lines=(
            PricedLine(
                sku="USD-1",
                cantidad=1,
                base_ars=Decimal(0),
                final_ars=Decimal(0),
                moneda="USD",
                source="RAG",
                name="USD item",
                supplier="AMX",
                precio_original=Decimal("10.00"),
                codigo_proveedor="AMX",
            ),
        ),
        conversion_pending=True,
    )

    order = persist_draft_order(session, customer, priced)

    assert order.conversion_pending is True
    assert order.subtotal is None
    assert order.total is None
    assert (
        session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id)).sku == "USD-1"
    )


def test_persist_draft_order_normalizes_doubled_prefix_sku(customer_ctx):
    """A doubled supplier prefix in a RAG SKU is stored collapsed to one prefix."""
    session, customer = customer_ctx
    priced = PricedOrder(
        lines=(
            PricedLine(
                sku="AMX-AMX-AT-5044",
                cantidad=1,
                base_ars=Decimal("120.00"),
                final_ars=Decimal("120.00"),
                moneda="ARS",
                source="RAG",
                name="RAG item",
                supplier="AMX",
                precio_original=Decimal("100.00"),
                codigo_proveedor="AMX",
            ),
        ),
        subtotal=Decimal("120.00"),
        total=Decimal("120.00"),
    )

    order = persist_draft_order(session, customer, priced)

    item = session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.sku == "AMX-AT-5044"
