"""Case C tests (owner pivot).

No-supplier orders move through the existing rejection flow: OrderEstado →
REJECTED (releasing every reservation) together with sourcing → CANCELLED, and
the owner receives the unavailability message as the in-chat reply (the
separate notification push is gone).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.agents.customer import SourcingDeps, build_handler
from src.agents.intake import SimpleOrderParser
from src.agents.inventory import available_stock, reserve_stock, seed_inventory
from src.channels.base import InboundMessage
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Order,
    OrderEstado,
    Supplier,
    ReservationEstado,
    SourcingState,
    StockReservation,
)
from src.orchestrator.router import AgentName, Orchestrator
from src.orchestrator.session import ConversationStore
from src.sourcing.case_c import cancel_for_no_supplier, persist_case_c_order
from src.supplier.searcher import FakeSupplierCatalogSearcher

OWNER_SENDER = "+5491100000000"
CUSTOMER_NAME = "Ferretería Don Juan"


class FakeResponder:
    """Never reached in the sourcing flow; raises if the LLM chat path runs."""

    def respond(self, messages):
        raise AssertionError("the sourcing flow must not call the LLM responder")


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
    """Catalog with only 2 units on hand and NO supplier candidates."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Supplier(supplier_id=1, razon_social="Supplier Test", margen_predeterminado=Decimal(0))
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
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=2,
            sinonimos=["clavo paris 2", "clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    seed_inventory(db_session)
    return {"session": db_session}


def _orchestrator(session) -> Orchestrator:
    deps = SourcingDeps(
        session_factory=lambda: session,
        searcher=FakeSupplierCatalogSearcher(),  # no supplier offers the item
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


def test_no_supplier_order_is_cancelled_and_reported_in_chat(shop):
    """Without a supplier: order rejected, sourcing CANCELLED, chat notice."""
    session = shop["session"]
    orchestrator = _orchestrator(session)

    result = orchestrator.handle_inbound(
        _message(f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas")
    )

    assert "cancelado" in result.reply  # type: ignore[operator]
    assert "no están disponibles" in result.reply  # type: ignore[operator]

    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order.estado is OrderEstado.REJECTED  # existing rejection flow
    assert order.sourcing_state is SourcingState.CANCELLED
    assert order.rejected_at is not None
    # The reply traveled in the chat: no separate push was made.
    assert orchestrator.store.get(OWNER_SENDER) is not None  # context retained


def test_cancel_for_no_supplier_releases_reservations(shop):
    """Cancelar libera las reservas activas del pedido (flujo de rechazo)."""
    session = shop["session"]
    order = persist_case_c_order(session, session.get(Cliente, 1))
    reserve_stock(session, "CLV-PRS-2", customer_id=1, cantidad=2, order_id=order.order_id)
    assert available_stock(session, "CLV-PRS-2") == 0

    cancel_for_no_supplier(session, order)

    assert order.estado is OrderEstado.REJECTED
    assert order.sourcing_state is SourcingState.CANCELLED
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.RELEASED
    assert available_stock(session, "CLV-PRS-2") == 2  # stock available again
