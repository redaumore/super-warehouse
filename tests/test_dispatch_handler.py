"""Wired DISPATCH handler tests (Phase 7).

The DISPATCH agent is wired: confirm/cancel replies run the real flow. These
tests drive ``build_dispatch_handler`` with a real session and a mock
``SheetsWriter``: confirm registers end-to-end, cancel releases reservations,
unknown replies re-ask, ``pedido #N`` overrides the rehydrated order, and a
Sheets QUARANTINE IS TOLERATED — the order stays CONFIRMED and the owner gets
an error surfaced in chat (spec: order MUST remain Confirmed).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.dispatch import build_dispatch_handler
from src.agents.inventory import available_stock, reserve_stock, seed_inventory
from src.channels.base import InboundMessage
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    ReservationEstado,
    SourcingNeed,
    StockReservation,
    Supplier,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)
from src.integrations.sheets import SheetsWriteStatus
from src.orchestrator.router import AgentOutcome
from src.orchestrator.session import ConversationState
from src.purchasing.accumulate import accumulate_need
from src.sourcing.persistence import upsert_sourcing_need

OWNER_SENDER = "+5491100000000"


class FakeSheets:
    """Sheets stand-in with an injectable outcome; records the appended rows."""

    def __init__(self, status: SheetsWriteStatus = SheetsWriteStatus.APPENDED) -> None:
        self.status = status
        self.rows: list[tuple[int, str]] = []

    def append_order_row(
        self, order_id, *, customer_name=None, total=None, items_summary="", registered_at=None
    ):
        self.rows.append((order_id, items_summary))
        return self.status


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


@pytest.fixture
def shop(db_session):
    """Seed a product, TWO customers and one DRAFT order per customer."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Supplier(
            id=1,
            code="TES",
            business_name="Test Supplier",
            default_margin_pct=Decimal(0),
        )
    )
    for customer_id, phone in ((1, "+5491155551234"), (2, "+5491155555678")):
        db_session.add(
            Cliente(
                customer_id=customer_id,
                nombre_comercial=f"Customer {customer_id}",
                telefono_norm=phone,
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
    orders = []
    # One DRAFT per customer — the single-draft rule forbids two DRAFTs for the
    # same customer (AD4), so the override test targets a different customer's.
    for customer_id in (1, 2):
        order = Order(customer_id=customer_id, estado=OrderEstado.DRAFT)
        db_session.add(order)
        db_session.flush()
        db_session.add(
            OrderItem(
                order_id=order.order_id,
                sku="CLV-001",
                cantidad=2,
                base_price=Decimal("100.00"),
                final_price=Decimal("100.00"),
                adjustment=Decimal(0),
                source="LOCAL",
            )
        )
        orders.append(order)
    db_session.flush()
    # Commit the seeds: the dispatch handler owns its own transaction per call,
    # and its rollback must only undo the handler's writes, never the fixtures.
    db_session.commit()
    return {"session": db_session, "orders": orders}


def _state(session_factory, order_id: int | None) -> ConversationState:
    return ConversationState(sender_id=OWNER_SENDER, order_id=order_id, awaiting_decision=True)


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel="whatsapp", sender_id=OWNER_SENDER, text=text)


def _on_hand(session, sku: str) -> int:
    row = session.scalar(select(Inventory).where(Inventory.sku_id == sku))
    assert row is not None
    return row.quantity_on_hand


def test_approve_confirms_order_and_registers(shop):
    """Confirmar registra el pedido en Sheets, descuenta stock y confirma en chat."""
    session = shop["session"]
    order = shop["orders"][0]
    reserve_stock(session, "CLV-001", customer_id=1, cantidad=2, order_id=order.order_id)
    sheets = FakeSheets()
    handler = build_dispatch_handler(lambda: session, sheets)

    outcome = handler(_message("aprobá"), _state(lambda: session, order.order_id), None)

    assert isinstance(outcome, AgentOutcome)
    assert "confirmado" in outcome.reply  # type: ignore[operator]
    assert "Registrado en Google Sheets" in outcome.reply  # type: ignore[operator]
    assert order.estado is OrderEstado.CONFIRMED
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.CONVERTED
    assert _on_hand(session, "CLV-001") == 8  # 10 − 2 deducted
    assert sheets.rows == [(order.order_id, "2 × CLV-001")]
    # The decision conversation is closed: awaiting_decision cleared.
    assert outcome.state is not None
    assert outcome.state.awaiting_decision is False
    assert outcome.state.order_id is None


def test_reject_cancels_order_and_releases_reservations(shop):
    """Rechazar cancela el pedido: reservas liberadas y stock disponible."""
    session = shop["session"]
    order = shop["orders"][0]
    reserve_stock(session, "CLV-001", customer_id=1, cantidad=2, order_id=order.order_id)
    assert available_stock(session, "CLV-001") == 8
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message("no, rechazá"), _state(lambda: session, order.order_id), None)

    assert "cancelado" in outcome.reply  # type: ignore[operator]
    assert order.estado is OrderEstado.CANCELED
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.RELEASED
    assert available_stock(session, "CLV-001") == 10


def test_reject_releases_auto_sourced_needs_and_cancels_the_empty_po(shop):
    """Rechazar un Confirmado con necesidad auto-sourced cancela el PO vaciado."""
    session = shop["session"]
    order = shop["orders"][0]
    order.estado = OrderEstado.CONFIRMED  # reject path runs from Confirmed too
    session.flush()
    need = upsert_sourcing_need(session, order.order_id, "CLV-001", 3)
    po = accumulate_need(session, need, 1)
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message("no, rechazá"), _state(lambda: session, order.order_id), None)

    assert "cancelado" in outcome.reply  # type: ignore[operator]
    assert order.estado is OrderEstado.CANCELED
    reloaded = session.get(SupplierPurchaseOrder, po.po_id)
    assert reloaded.estado is SupplierPurchaseOrderState.CANCELLED
    assert session.scalars(select(SupplierPurchaseOrderItem)).all() == []
    reloaded_need = session.get(SourcingNeed, need.need_id)
    assert reloaded_need.po_item_id is None  # detached: no phantom PO quantities


def test_unknown_decision_asks_again_without_touching_order(shop):
    """Una respuesta que no es confirmar/cancelar re-pregunta sin tocar el pedido."""
    session = shop["session"]
    order = shop["orders"][0]
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message("hablamos mañana"), _state(lambda: session, order.order_id), None)

    assert "aprobá" in outcome.reply  # type: ignore[operator]
    assert order.estado is OrderEstado.DRAFT
    assert outcome.state is not None
    assert outcome.state.awaiting_decision is True  # still awaiting


def test_order_number_override_targets_specific_order(shop):
    """La referencia 'pedido #N' decide sobre ese pedido, no el del estado."""
    session = shop["session"]
    first, second = shop["orders"]
    # The state points at the LATEST order, but the owner references #1.
    state = _state(lambda: session, second.order_id)
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message(f"aprobá el pedido #{first.order_id}"), state, None)

    assert first.estado is OrderEstado.CONFIRMED
    assert second.estado is OrderEstado.DRAFT  # untouched
    assert "confirmado" in outcome.reply  # type: ignore[operator]


def test_unknown_order_number_errors_cleanly(shop):
    """Una referencia a un pedido inexistente responde un error claro."""
    session = shop["session"]
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message("aprobá el pedido #999"), _state(lambda: session, None), None)

    assert "No encuentro el pedido #999" in outcome.reply  # type: ignore[operator]


def test_sheets_quarantine_is_tolerated_and_order_stays_confirmed(shop):
    """Si Sheets no registra, el pedido IGUAL queda Confirmado y se informa."""
    session = shop["session"]
    order = shop["orders"][0]
    order_id = order.order_id  # the handler closes the session and expires objects
    reserve_stock(session, "CLV-001", customer_id=1, cantidad=2, order_id=order_id)
    session.commit()  # the reservation survives the handler's own transaction
    sheets = FakeSheets(status=SheetsWriteStatus.QUARANTINED)
    handler = build_dispatch_handler(lambda: session, sheets)

    outcome = handler(_message("aprobá"), _state(lambda: session, order_id), None)

    assert "cuarentena" in outcome.reply  # type: ignore[operator]  # failure surfaced
    # The order stayed CONFIRMED (spec) and the stock was still deducted.
    fresh_order = session.scalar(select(Order).where(Order.order_id == order_id))
    assert fresh_order.estado is OrderEstado.CONFIRMED
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order_id)
    )
    assert reservation.estado is ReservationEstado.CONVERTED
    assert _on_hand(session, "CLV-001") == 8  # deducted despite the quarantine
    assert outcome.state is not None
    assert outcome.state.awaiting_decision is False  # the decision is closed


def test_dispatch_handler_logs_session_events(shop, tmp_path, monkeypatch):
    """El handler de dispatch y la ceremonia de approval emiten eventos de sesión estructurados."""
    from src.observability.session_logger import read_session_events, set_current_session_id

    monkeypatch.setattr("src.observability.session_logger.DEFAULT_SESSIONS_DIR", tmp_path)
    sid = "ses_dispatch_test"
    set_current_session_id(sid)
    try:
        session = shop["session"]
        order = shop["orders"][0]
        order_id = order.order_id
        reserve_stock(session, "CLV-001", customer_id=1, cantidad=2, order_id=order_id)
        session.commit()

        handler = build_dispatch_handler(lambda: session, FakeSheets())
        handler(_message("aprobá"), _state(lambda: session, order_id), None)

        events = read_session_events(sid, log_dir=tmp_path)
        actions = [e["action"] for e in events]
        assert "decision_parsed" in actions
        assert "order_classified" in actions
        assert "order_confirmed_case_a" in actions
        assert "decision_approved" in actions
    finally:
        set_current_session_id(None)
