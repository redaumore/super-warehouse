"""Wired DISPATCH handler tests (task 3.4).

The DISPATCH agent is no longer a stub: approve/reject replies run the real
approval flow. These tests drive ``build_dispatch_handler`` with a real
session and a mock ``SheetsWriter``: approve registers end-to-end, reject
releases reservations, unknown replies re-ask, ``pedido #N`` overrides the
rehydrated order, and a Sheets QUARANTINE rolls the approval back — the order
stays PENDING and the owner gets an error reply.
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
    StockReservation,
    Supplier,
)
from src.integrations.sheets import SheetsWriteStatus
from src.orchestrator.router import AgentOutcome
from src.orchestrator.session import ConversationState

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
                "catalogo, suppliers, clientes, lista_precios RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def shop(db_session):
    """Seed a customer, product and TWO PENDING_APPROVAL orders."""
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
    orders = []
    for _ in range(2):
        order = Order(customer_id=1, estado=OrderEstado.PENDING_APPROVAL)
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


def test_approve_registers_order_and_confirms(shop):
    """Aprobar registra el pedido en Sheets, descuenta stock y confirma en chat."""
    session = shop["session"]
    order = shop["orders"][0]
    reserve_stock(session, "CLV-001", customer_id=1, cantidad=2, order_id=order.order_id)
    sheets = FakeSheets()
    handler = build_dispatch_handler(lambda: session, sheets)

    outcome = handler(_message("aprobá"), _state(lambda: session, order.order_id), None)

    assert isinstance(outcome, AgentOutcome)
    assert "aprobado" in outcome.reply  # type: ignore[operator]
    assert "Registrado en Google Sheets" in outcome.reply  # type: ignore[operator]
    assert order.estado is OrderEstado.APPROVED
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


def test_reject_releases_reservations(shop):
    """Rechazar libera las reservas y el stock vuelve a estar disponible."""
    session = shop["session"]
    order = shop["orders"][0]
    reserve_stock(session, "CLV-001", customer_id=1, cantidad=2, order_id=order.order_id)
    assert available_stock(session, "CLV-001") == 8
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message("no, rechazá"), _state(lambda: session, order.order_id), None)

    assert "rechazado" in outcome.reply  # type: ignore[operator]
    assert order.estado is OrderEstado.REJECTED
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.RELEASED
    assert available_stock(session, "CLV-001") == 10


def test_unknown_decision_asks_again_without_touching_order(shop):
    """Una respuesta que no es aprobar/rechazar re-pregunta sin tocar el pedido."""
    session = shop["session"]
    order = shop["orders"][0]
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message("hablamos mañana"), _state(lambda: session, order.order_id), None)

    assert "aprobá" in outcome.reply  # type: ignore[operator]
    assert order.estado is OrderEstado.PENDING_APPROVAL
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

    assert first.estado is OrderEstado.APPROVED
    assert second.estado is OrderEstado.PENDING_APPROVAL  # untouched
    assert "aprobado" in outcome.reply  # type: ignore[operator]


def test_unknown_order_number_errors_cleanly(shop):
    """Una referencia a un pedido inexistente responde un error claro."""
    session = shop["session"]
    handler = build_dispatch_handler(lambda: session, FakeSheets())

    outcome = handler(_message("aprobá el pedido #999"), _state(lambda: session, None), None)

    assert "No encuentro el pedido #999" in outcome.reply  # type: ignore[operator]


def test_sheets_quarantine_rolls_back_approval(shop):
    """Si Sheets no registra, la aprobación se revierte: el pedido sigue pendiente."""
    session = shop["session"]
    order = shop["orders"][0]
    order_id = order.order_id  # the handler closes the session and expires objects
    reserve_stock(session, "CLV-001", customer_id=1, cantidad=2, order_id=order_id)
    session.commit()  # the reservation survives the handler's own rollback
    sheets = FakeSheets(status=SheetsWriteStatus.QUARANTINED)
    handler = build_dispatch_handler(lambda: session, sheets)

    outcome = handler(_message("aprobá"), _state(lambda: session, order_id), None)

    assert "sigue pendiente" in outcome.reply  # type: ignore[operator]
    # The handler's rollback reverted the approval: fresh reads prove the order
    # stayed PENDING, the reservation ACTIVE and the stock undeducted.
    fresh_order = session.scalar(select(Order).where(Order.order_id == order_id))
    assert fresh_order.estado is OrderEstado.PENDING_APPROVAL
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order_id)
    )
    assert reservation.estado is ReservationEstado.ACTIVE  # not converted
    assert _on_hand(session, "CLV-001") == 10  # nothing deducted
    # The decision conversation stays open so the owner can retry.
    assert outcome.state is not None
    assert outcome.state.awaiting_decision is True
