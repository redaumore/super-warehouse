"""Confirm ceremony orchestration tests (Phase 4).

Unit (no DB): order totals and items summaries over ORM-shaped objects, and
the sheets-append boundary (the ceremony appends once; draft persistence never
touches Sheets).

Integration (Postgres, skipped when down): the full confirm flow — lifecycle
transition, reservation conversion, stock deduction, Sheets append and the
in-chat confirmation — plus the failure and tolerance paths: a stale order
refuses, a second confirm is an ``InvalidTransitionError``, and a Sheets
QUARANTINE IS TOLERATED (the order stays CONFIRMED, the status is surfaced, no
rollback — spec order-lifecycle).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.inventory import reserve_stock, seed_inventory
from src.config import Settings, get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    ReservationEstado,
    StockReservation,
    Supplier,
)
from src.integrations.sheets import SheetsWriter, SheetsWriteStatus
from src.orchestrator.approval import (
    PendingConversionError,
    build_items_summary,
    confirm_and_register,
    order_total,
)
from src.order_lifecycle.state import InvalidTransitionError, RequiresRequoteError
from src.pricing.order_pricing import PricedLine, PricedOrder
from src.sourcing.draft_order import persist_draft_order

# ---------------------------------------------------------------- unit tests


def test_order_total_sums_final_price_times_quantity():
    """El total del pedido suma precio final por cantidad, redondeado a centavos."""
    order = SimpleNamespace(
        items=[
            SimpleNamespace(sku="A", final_price=Decimal("100.00"), cantidad=2),
            SimpleNamespace(sku="B", final_price=Decimal("50.50"), cantidad=3),
        ]
    )
    assert order_total(order) == Decimal("351.50")


def test_order_total_with_adjusted_line():
    """Una línea ajustada aporta su precio final rebajado al total."""
    order = SimpleNamespace(
        items=[SimpleNamespace(sku="A", final_price=Decimal("95.00"), cantidad=1)]
    )
    assert order_total(order) == Decimal("95.00")


def test_build_items_summary_lists_each_line():
    """El resumen de ítems lista cantidad por SKU separado por punto y coma."""
    order = SimpleNamespace(
        items=[
            SimpleNamespace(sku="CLV-001", cantidad=10),
            SimpleNamespace(sku="TRN-002", cantidad=5),
        ]
    )
    assert build_items_summary(order) == "10 × CLV-001; 5 × TRN-002"


def test_sheets_append_belongs_to_confirm_not_draft_persistence():
    """Sheets append is skipped at draft save and called once during confirm."""
    save_session = Mock()
    customer = SimpleNamespace(customer_id=7)
    priced = PricedOrder(
        lines=(
            PricedLine(
                sku="RAG-1",
                cantidad=1,
                base_ars=Decimal("100.00"),
                final_ars=Decimal("100.00"),
                moneda="ARS",
                source="RAG",
                name="RAG item",
                supplier="SUP",
                precio_original=Decimal("100.00"),
            ),
        ),
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
    )

    with patch.object(SheetsWriter, "append_order_row") as append_at_save:
        persist_draft_order(save_session, customer, priced)

    append_at_save.assert_not_called()


# -------------------------------------------------- integration (confirm flow)


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
def _clean_schema(db_engine):
    yield
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE supplier_purchase_order_items, supplier_purchase_orders, "
                "sourcing_needs, inventory, order_items, orders, stock_reservations, "
                "stock_adjustments, catalogo, suppliers, clientes, lista_precios "
                "RESTART IDENTITY CASCADE"
            )
        )


def _on_hand(session, sku: str) -> int:
    """Read a SKU's canonical on-hand quantity from Inventory."""
    row = session.scalar(select(Inventory).where(Inventory.sku_id == sku))
    assert row is not None
    return row.quantity_on_hand


@pytest.fixture
def order_ctx(db_session):
    """Seed product (10 units), customer, DRAFT order + one item."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Supplier(
            id=1,
            code="TES",
            business_name="Test Supplier",
            default_margin_pct=Decimal(0),
        )
    )
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial="Ferretería Don Juan",
            telefono_norm="+5491155551234",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-001",
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=["clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    seed_inventory(db_session)
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, needs_requote=False)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="CLV-001",
            cantidad=10,
            base_price=Decimal("135.00"),
            final_price=Decimal("135.00"),
            adjustment=Decimal(0),
            source="LOCAL",
        )
    )
    db_session.flush()
    return {"session": db_session, "order": order, "sku": "CLV-001"}


def _sheets_writer() -> SheetsWriter:
    # No credentials: append quarantines internally without touching the network.
    return SheetsWriter(gc=None, settings=Settings(google_sheets_credentials_file=""))


class FakeSheets:
    """Sheets stand-in that always succeeds: records the appended rows."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []

    def append_order_row(
        self, order_id, *, customer_name=None, total=None, items_summary="", registered_at=None
    ):
        self.rows.append((order_id, items_summary))
        return SheetsWriteStatus.APPENDED


def test_confirm_and_register_converts_deducts_and_confirms(order_ctx):
    """Confirmar registra: convierte reservas, descuenta stock, agrega a Sheets y confirma."""
    reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=10,
        order_id=order_ctx["order"].order_id,
    )
    result = confirm_and_register(
        order_ctx["session"],
        order_ctx["order"],
        sheets=FakeSheets(),
        customer_name="Ferretería Don Juan",
    )
    assert result.order.estado is OrderEstado.CONFIRMED
    assert result.converted == 1
    assert result.total == Decimal("1350.00")
    assert "confirmado" in result.confirmation_text
    assert "Pedido #" in result.confirmation_text
    reservations = (
        order_ctx["session"]
        .scalars(
            select(StockReservation).where(StockReservation.order_id == order_ctx["order"].order_id)
        )
        .all()
    )
    assert all(r.estado is ReservationEstado.CONVERTED for r in reservations)
    assert _on_hand(order_ctx["session"], "CLV-001") == 0  # 10 − 10
    # Legacy catalogo stock counter is untouched by the confirm deduction.
    assert order_ctx["session"].get(Catalogo, 1).stock_disponible == 10


def test_confirm_on_expired_reservation_refuses_without_side_effects(order_ctx):
    """Confirmar una reserva vencida exige recotizar y no produce efectos laterales."""
    reservation = reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    reservation.timestamp = datetime.now(UTC) - timedelta(minutes=31)
    order_ctx["session"].flush()
    with pytest.raises(RequiresRequoteError):
        confirm_and_register(order_ctx["session"], order_ctx["order"], sheets=FakeSheets())
    assert order_ctx["order"].estado is OrderEstado.DRAFT
    assert order_ctx["order"].needs_requote is True
    reservation = order_ctx["session"].get(StockReservation, reservation.reservation_id)
    assert reservation.estado is ReservationEstado.ACTIVE
    assert _on_hand(order_ctx["session"], "CLV-001") == 10  # nothing deducted


def test_second_confirm_is_an_invalid_transition(order_ctx):
    """Confirmar dos veces es una transición inválida (idempotencia del ceremonia)."""
    confirm_and_register(order_ctx["session"], order_ctx["order"], sheets=FakeSheets())
    with pytest.raises(InvalidTransitionError, match="cannot confirm"):
        confirm_and_register(order_ctx["session"], order_ctx["order"], sheets=FakeSheets())


def test_sheets_quarantine_is_tolerated_and_order_stays_confirmed(order_ctx):
    """La cuarentena de Sheets NO revierte: el pedido queda Confirmado y se informa."""
    reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=10,
        order_id=order_ctx["order"].order_id,
    )
    result = confirm_and_register(
        order_ctx["session"],
        order_ctx["order"],
        sheets=_sheets_writer(),  # no credentials → append quarantines
    )
    assert result.order.estado is OrderEstado.CONFIRMED  # spec: order stays Confirmed
    assert result.sheets_status is SheetsWriteStatus.QUARANTINED
    assert "cuarentena" in result.confirmation_text  # failure surfaced to the owner
    assert _on_hand(order_ctx["session"], "CLV-001") == 0  # stock was still deducted
    reservations = (
        order_ctx["session"]
        .scalars(
            select(StockReservation).where(StockReservation.order_id == order_ctx["order"].order_id)
        )
        .all()
    )
    assert all(r.estado is ReservationEstado.CONVERTED for r in reservations)


def test_confirm_pending_conversion_order_is_blocked(order_ctx):
    """Confirmar un pedido con precios pendientes de conversión se bloquea."""
    order = order_ctx["order"]
    order.conversion_pending = True
    order_ctx["session"].flush()
    with pytest.raises(PendingConversionError, match="pending currency conversion"):
        confirm_and_register(order_ctx["session"], order, sheets=FakeSheets())
    assert order.estado is OrderEstado.DRAFT


def test_confirm_discovering_case_c_cancels_the_order(order_ctx):
    """Classify at confirm: stock que cayó sin supplier cancela el pedido (Case C)."""
    # The order's item exceeds the on-hand stock at confirm: 12 requested, 8 on hand.
    item = order_ctx["session"].scalar(
        select(OrderItem).where(OrderItem.order_id == order_ctx["order"].order_id)
    )
    item.cantidad = 12
    order_ctx["session"].flush()
    on_hand = order_ctx["session"].scalar(
        select(Inventory).where(Inventory.sku_id == order_ctx["sku"])
    )
    on_hand.quantity_on_hand = 8
    order_ctx["session"].flush()

    result = confirm_and_register(
        order_ctx["session"], order_ctx["order"], sheets=FakeSheets()
    )  # no searcher → no supplier candidates → Case C

    assert result.cancelled_case is True
    assert result.order.estado is OrderEstado.CANCELED
    assert result.order.sourcing_state.value == "CANCELLED"
    assert "no están disponibles" in result.confirmation_text
    assert result.converted == 0
    assert result.sheets_status is SheetsWriteStatus.SKIPPED


def test_confirm_discovering_case_b_persists_needs_and_returns_selection_prompt(order_ctx):
    """Classify at confirm: stock que cayó con suppliers devuelve la selección (Case B)."""
    from src.supplier.searcher import FakeSupplierCatalogSearcher, SupplierCandidate

    item = order_ctx["session"].scalar(
        select(OrderItem).where(OrderItem.order_id == order_ctx["order"].order_id)
    )
    item.cantidad = 12
    order_ctx["session"].flush()
    on_hand = order_ctx["session"].scalar(
        select(Inventory).where(Inventory.sku_id == order_ctx["sku"])
    )
    on_hand.quantity_on_hand = 8
    order_ctx["session"].flush()
    candidates = (
        SupplierCandidate(
            supplier_id=1,
            business_name="Supplier X",
            sku="CLV-001",
            description="Clavos Paris 2 Pulgadas",
            available_quantity=50,
        ),
    )
    searcher = FakeSupplierCatalogSearcher(candidates)

    result = confirm_and_register(
        order_ctx["session"], order_ctx["order"], sheets=FakeSheets(), searcher=searcher
    )

    assert result.cancelled_case is False
    assert result.order.estado is OrderEstado.CONFIRMED  # state independent of PO progress
    assert result.converted == 0
    assert result.missing
    assert result.missing[0].missing_quantity == 4  # 12 − 8
    assert "faltan 4" in result.confirmation_text
    from src.db.models import SourcingNeed

    need = order_ctx["session"].scalar(
        select(SourcingNeed).where(SourcingNeed.order_id == order_ctx["order"].order_id)
    )
    assert need is not None
    assert need.missing_quantity == 4
