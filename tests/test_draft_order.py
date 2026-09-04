"""Integration tests for persistence of source-aware customer draft orders.

The draft is persisted as an ``Order`` with ``estado=DRAFT`` at the first add
that knows the customer (design AD2). ``persist_draft_order`` writes NO
reservations — the ACTIVE soft-lock is created at the quote step (AD10) by the
customer handler; the confirm ceremony converts and deducts. These tests also
prove the single-draft rule: the app guard refuses a second draft and the
``uq_orders_one_draft_per_customer`` partial index makes the concurrent race
fail cleanly with exactly one survivor.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from src.agents.customer import _supplier_margin_source
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    StockReservation,
    Supplier,
)
from src.order_lifecycle.state import add_draft_item, remove_draft_item
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


def _priced_local(cantidad: int = 2) -> PricedOrder:
    return PricedOrder(
        lines=(
            PricedLine(
                sku="LOCAL-1",
                cantidad=cantidad,
                base_ars=Decimal("135.00"),
                final_ars=Decimal("135.00"),
                moneda="ARS",
                source="LOCAL",
                name="Local item",
                precio_original=Decimal("100.00"),
            ),
        ),
        subtotal=Decimal("270.00") if cantidad == 2 else Decimal("135.00"),
        total=Decimal("270.00") if cantidad == 2 else Decimal("135.00"),
    )


def test_persist_draft_order_writes_draft_without_reservations(customer_ctx):
    """Persist crea un Order DRAFT con sus líneas y sin reservar stock."""
    session, customer = customer_ctx
    order = persist_draft_order(session, customer, _priced_local())

    assert order.estado is OrderEstado.DRAFT
    assert order.subtotal == Decimal("270.00")
    assert order.total == Decimal("270.00")
    # AD10: no reservations at persist — the quote step soft-locks later.
    assert (
        session.scalars(
            select(StockReservation).where(StockReservation.order_id == order.order_id)
        ).all()
        == []
    )
    items = session.scalars(select(OrderItem).where(OrderItem.order_id == order.order_id)).all()
    assert items[0].sku == "LOCAL-1"
    assert items[0].source == "LOCAL"


def test_persist_draft_order_keeps_rag_snapshot_without_reservation(customer_ctx):
    """RAG lines remain catalog-independent snapshots; never reserved."""
    session, customer = customer_ctx
    priced = PricedOrder(
        lines=(
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
        subtotal=Decimal("487.80"),
        total=Decimal("487.80"),
    )

    order = persist_draft_order(session, customer, priced)

    assert order.estado is OrderEstado.DRAFT
    assert session.scalars(select(StockReservation)).all() == []
    item = session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.sku == "AMX-AT-5044"
    assert item.source == "RAG"
    assert item.name == "RAG item"
    assert item.precio_original == Decimal("135.5000")


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

    assert order.estado is OrderEstado.DRAFT
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


# ------------------------------------------------- single-draft rule (AD4)


def test_second_draft_for_same_customer_is_rejected_and_preserved(customer_ctx):
    """A second DRAFT for a customer with one open is rejected; the first survives."""
    session, customer = customer_ctx
    first = persist_draft_order(session, customer, _priced_local())
    session.commit()

    with pytest.raises(IntegrityError):
        persist_draft_order(session, customer, _priced_local(cantidad=1))
    session.rollback()

    orders = session.scalars(select(Order)).all()
    assert len(orders) == 1
    assert orders[0].order_id == first.order_id
    assert orders[0].estado is OrderEstado.DRAFT


def test_two_session_draft_race_exactly_one_survives(db_engine, customer_ctx):
    """Concurrent draft adds: exactly one DRAFT survives; the other fails cleanly."""
    session, customer = customer_ctx
    persist_draft_order(session, customer, _priced_local())
    session.commit()

    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    second_session = Session()
    try:
        with pytest.raises(IntegrityError):
            persist_draft_order(second_session, customer, _priced_local(cantidad=1))
    finally:
        second_session.rollback()
        second_session.close()

    assert session.scalar(select(func.count(Order.order_id))) == 1


# ------------------------------------------- draft line edits across sessions


def test_remove_draft_item_is_real_on_persisted_draft(customer_ctx):
    """remove borra la OrderItem; el Draft vacío sigue DRAFT."""
    session, customer = customer_ctx
    order = persist_draft_order(session, customer, _priced_local())
    remove_draft_item(session, order, "LOCAL-1")

    assert session.scalars(select(OrderItem)).all() == []
    assert order.estado is OrderEstado.DRAFT


def test_add_draft_item_after_resume_appends_to_same_draft(customer_ctx):
    """Un Draft persistido retomado en otra sesión acepta nuevas líneas."""
    session, customer = customer_ctx
    order = persist_draft_order(session, customer, _priced_local())
    order_id = order.order_id
    session.commit()

    Session = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    resumed = Session()
    try:
        resumed_order = resumed.get(Order, order_id)
        assert resumed_order.estado is OrderEstado.DRAFT
        add_draft_item(resumed, resumed_order, "TRN-002", 2)
        resumed.commit()
    finally:
        resumed.close()

    items = session.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.sku)
    ).all()
    assert [(i.sku, i.cantidad) for i in items] == [("LOCAL-1", 2), ("TRN-002", 2)]
