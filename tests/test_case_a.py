"""Case A orchestrator e2e tests (owner pivot).

Drives the sourcing flow through the real orchestrator with a fake responder,
a fake (empty) supplier searcher and the owner sender: a full-stock text order
naming its customer is parsed, the customer is resolved by NAME, classified
Case A, persisted through the existing reservation + quotation flow with
sourcing PENDING_ASSEMBLY and the delivery date stored, and the quote is the
agent's IN-CHAT reply (no owner push — the notifier bridge is gone).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.agents.customer import SourcingDeps, build_handler
from src.agents.intake import SimpleOrderParser
from src.agents.inventory import available_stock
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
    Proveedor,
    ReservationEstado,
    SourcingState,
    StockReservation,
)
from src.integrations.sheets import SheetsWriteStatus
from src.orchestrator.router import AgentName, Orchestrator
from src.orchestrator.session import ConversationStore
from src.supplier.searcher import FakeSupplierCatalogSearcher

OWNER_SENDER = "+5491100000000"
CUSTOMER_NAME = "Ferretería Don Juan"


class FakeResponder:
    """Never reached in the sourcing flow; raises if the LLM chat path runs."""

    def respond(self, messages):
        raise AssertionError("the sourcing flow must not call the LLM responder")


class FakeSheets:
    """Append-only Sheets stand-in: records rows, always APPENDED."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []

    def append_order_row(
        self, order_id, *, customer_name=None, total=None, items_summary="", registered_at=None
    ):
        self.rows.append((order_id, items_summary))
        return SheetsWriteStatus.APPENDED


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
def shop(db_session):
    """Catalog with 50 units on hand, a customer and a supplier."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Proveedor(proveedor_id=1, razon_social="Proveedor Test", margen_predeterminado=Decimal(0))
    )
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial=CUSTOMER_NAME,
            telefono_norm="+5491155551234",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-PRS-2",
            proveedor_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=50,
            sinonimos=["clavo paris 2", "clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    from src.agents.inventory import seed_inventory

    seed_inventory(db_session)
    return {"session": db_session}


def _orchestrator(session) -> Orchestrator:
    deps = SourcingDeps(
        session_factory=lambda: session,
        searcher=FakeSupplierCatalogSearcher(),
    )
    store = ConversationStore()
    orchestrator = Orchestrator(store, parser=SimpleOrderParser())
    orchestrator.register(
        AgentName.CUSTOMER,
        build_handler(FakeResponder(), sourcing=deps),  # type: ignore[arg-type]
    )
    return orchestrator


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel="whatsapp", sender_id=OWNER_SENDER, text=text)


def test_full_stock_order_flows_through_case_a(shop):
    """Un pedido con stock completo crea la orden Case A y cotiza en el chat del dueño."""
    session = shop["session"]
    orchestrator = _orchestrator(session)

    result = orchestrator.handle_inbound(
        _message(f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas para el viernes")
    )

    assert result.decision.agent is AgentName.CUSTOMER
    assert result.decision.parsed is True
    assert "Pedido #1 de Ferretería Don Juan confirmado" in result.reply  # type: ignore[operator]
    assert "aprobá" in result.reply  # type: ignore[operator]  # in-chat quote asks approval
    assert "2026" in result.reply  # type: ignore[operator]  # delivery date shown

    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order is not None
    assert order.estado is OrderEstado.PENDING_APPROVAL
    assert order.sourcing_state is SourcingState.PENDING_ASSEMBLY
    assert order.delivery_date is not None  # fuzzy "para el viernes" resolved

    # Standard reservation with the configured TTL (approval flow unchanged).
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation is not None
    assert reservation.estado is ReservationEstado.ACTIVE
    assert reservation.ttl_minutes == get_settings().reservation_ttl_minutes
    assert available_stock(session, "CLV-PRS-2") == 40

    # Priced order items were persisted through the quotation flow.
    item = session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.final_price == Decimal("135.00")
    assert item.cantidad == 10

    # The conversation is awaiting the decision → the next owner reply routes
    # to Dispatch (no external push was made).
    state = orchestrator.store.get(OWNER_SENDER)
    assert state is not None
    assert state.awaiting_decision is True
    assert state.order_id == order.order_id


def test_case_a_order_can_be_approved_with_stock_deduction(shop):
    """La aprobación de un pedido Case A descuenta el Inventory canónico."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    orchestrator.handle_inbound(_message(f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas"))

    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    from src.agents.dispatch import Decision, DecisionAction, apply_decision
    from src.orchestrator.approval import register_approved_order

    apply_decision(session, order, Decision(action=DecisionAction.APPROVE))
    result = register_approved_order(session, order, sheets=FakeSheets())
    assert result.order.estado is OrderEstado.APPROVED
    assert "aprobado" in result.confirmation_text
    assert "Registrado en Google Sheets" in result.confirmation_text
    on_hand = session.scalar(select(Inventory).where(Inventory.sku_id == "CLV-PRS-2"))
    assert on_hand.quantity_on_hand == 40  # 50 − 10


def test_case_a_reservation_ttl_requote_rules_unchanged(shop):
    """Las reglas de TTL/recotización aplican igual a un pedido Case A."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    orchestrator.handle_inbound(_message(f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas"))

    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    reservation.timestamp = datetime.now(UTC) - timedelta(minutes=31)
    session.flush()

    from src.agents.dispatch import Decision, DecisionAction, apply_decision
    from src.order_lifecycle.state import RequiresRequoteError

    with pytest.raises(RequiresRequoteError):
        apply_decision(session, order, Decision(action=DecisionAction.APPROVE))
    assert order.needs_requote is True
    assert order.estado is OrderEstado.PENDING_APPROVAL
    assert available_stock(session, "CLV-PRS-2") == 50  # expired lock freed


def test_case_a_unknown_customer_name_offers_creation(shop):
    """Un nombre de cliente desconocido ofrece crearlo en chat, sin crear orden."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    result = orchestrator.handle_inbound(_message("para Almacén La Esquina quiero 10 clavos"))
    assert "nuevo cliente" in result.reply  # type: ignore[operator]
    assert session.scalar(select(Order)) is None


def test_case_a_order_without_customer_name_asks_for_it(shop):
    """Un pedido sin nombre de cliente pide identificarlo antes de continuar."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    result = orchestrator.handle_inbound(_message("quiero 10 clavos"))
    assert "¿Para qué cliente" in result.reply  # type: ignore[operator]
    assert session.scalar(select(Order)) is None
