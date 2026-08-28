"""Case B orchestrator e2e tests (owner pivot).

Drives the multi-turn sourcing flow through the real orchestrator: a partial
stock order NAMING its customer is parsed, the customer is resolved by name,
classified Case B, persisted with sourcing IN_PREPARATION and its SourcingNeed
rows, and the reply lists the missing items with numbered supplier options. The
owner's numbered reply routes to the SOURCING agent, which accumulates the
selection into one OPEN PO per supplier; re-selection before execution moves
the need between POs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.agents.customer import SourcingDeps, build_handler
from src.agents.intake import SimpleOrderParser
from src.agents.inventory import seed_inventory
from src.channels.base import InboundMessage
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Order,
    SourcingNeed,
    SourcingState,
    Supplier,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)
from src.orchestrator.router import AgentName, Orchestrator
from src.orchestrator.session import ConversationStore, rehydrate_conversation
from src.sourcing.case_b import build_sourcing_handler
from src.supplier.searcher import FakeSupplierCatalogSearcher, SupplierCandidate

OWNER_SENDER = "+5491100000000"
CUSTOMER_NAME = "Ferretería Don Juan"
ORDER_MESSAGE = f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas"

CANDIDATES = (
    SupplierCandidate(
        supplier_id=1,
        business_name="Supplier X",
        sku="CLV-PRS-2",
        description="Clavos Paris 2 Pulgadas",
        available_quantity=50,
    ),
    SupplierCandidate(
        supplier_id=2,
        business_name="Supplier Y",
        sku="CLV-PRS-2",
        description="Clavos Paris 2 Pulgadas",
        available_quantity=30,
    ),
)


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
    """Catalog with only 4 units on hand → 10 requested leaves 6 missing."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Supplier(id=1, code="SUP", business_name="Supplier X", default_margin_pct=Decimal(0))
    )
    db_session.add(
        Supplier(id=2, code="SUY", business_name="Supplier Y", default_margin_pct=Decimal(0))
    )
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial="Ferretería Don Juan",
            telefono_norm=OWNER_SENDER,
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
            stock_disponible=4,
            sinonimos=["clavo paris 2", "clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    seed_inventory(db_session)
    return {"session": db_session}


def _orchestrator(session) -> Orchestrator:
    deps = SourcingDeps(
        session_factory=lambda: session,
        searcher=FakeSupplierCatalogSearcher(CANDIDATES),
    )
    store = ConversationStore(
        rehydrator=lambda sid: rehydrate_conversation(session, sid, searcher=deps.searcher)
    )
    orchestrator = Orchestrator(store, parser=SimpleOrderParser())
    orchestrator.register(
        AgentName.CUSTOMER,
        build_handler(FakeResponder(), sourcing=deps),  # type: ignore[arg-type]
    )
    orchestrator.register(AgentName.SOURCING, build_sourcing_handler(lambda: session))
    return orchestrator


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel="whatsapp", sender_id=OWNER_SENDER, text=text)


def test_partial_order_lists_missing_items_and_suppliers(shop):
    """A partial order lists the missing items and the numbered suppliers."""
    session = shop["session"]
    orchestrator = _orchestrator(session)

    result = orchestrator.handle_inbound(_message(ORDER_MESSAGE))

    assert result.decision.parsed is True
    reply = result.reply
    assert "faltan 6" in reply  # type: ignore[operator]
    assert "1) Supplier X" in reply  # type: ignore[operator]
    assert "2) Supplier Y" in reply  # type: ignore[operator]

    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order.sourcing_state is SourcingState.IN_PREPARATION  # set at detection
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need.sku == "CLV-PRS-2"
    assert need.missing_quantity == 6
    assert need.supplier_id is None  # pending selection
    # No approval wait: the selection conversation owns the next turn.
    state = orchestrator.store.get(OWNER_SENDER)
    assert state.sourcing_selection_pending is True
    assert {c.supplier_id for c in state.sourcing_candidates} == {1, 2}


def test_owner_selection_accumulates_open_po(shop):
    """The owner's choice accumulates an OPEN PO for the picked supplier."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    orchestrator.handle_inbound(_message(ORDER_MESSAGE))

    result = orchestrator.handle_inbound(_message("1"))

    assert result.decision.agent is AgentName.SOURCING
    assert "PO #1" in result.reply  # type: ignore[operator]
    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order.sourcing_state is SourcingState.IN_PREPARATION
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need.supplier_id == 1

    po = session.scalar(select(SupplierPurchaseOrder).where(SupplierPurchaseOrder.supplier_id == 1))
    assert po is not None
    assert po.estado is SupplierPurchaseOrderState.OPEN
    item = session.scalar(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == po.po_id)
    )
    assert item.sku == "CLV-PRS-2"
    assert item.quantity == 6
    # The selection phase stays open until the PO is executed (re-selection).
    assert orchestrator.store.get(OWNER_SENDER).sourcing_selection_pending is True


def test_reselection_before_execution_moves_need_between_pos(shop):
    """Re-selecting a supplier before execution moves the need between POs."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    orchestrator.handle_inbound(_message(ORDER_MESSAGE))
    orchestrator.handle_inbound(_message("1"))  # supplier X first

    result = orchestrator.handle_inbound(_message("2"))  # owner changes to Y

    assert "PO #2" in result.reply  # type: ignore[operator]
    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need.supplier_id == 2
    # The old X PO lost the line (no double ordering).
    old_items = session.scalars(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == 1)
    ).all()
    assert old_items == []
    new_item = session.scalar(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == 2)
    )
    assert new_item.quantity == 6
    assert need.po_item_id == new_item.po_item_id


def test_selection_survives_ttl_in_orchestrator_flow(shop):
    """Tras el TTL, la selección continúa rehidratada desde la DB."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    orchestrator.handle_inbound(_message(ORDER_MESSAGE))

    # Expire the in-memory state; the store rehydrates from the DB.
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    state = orchestrator.store.get(OWNER_SENDER)
    state.updated_at = now - timedelta(minutes=31)
    orchestrator.store.put(state)

    result = orchestrator.handle_inbound(_message("2"))

    assert result.decision.agent is AgentName.SOURCING
    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need.supplier_id == 2


def test_invalid_selection_number_asks_again(shop):
    """Un número fuera de rango no acumula y pide repetir."""
    session = shop["session"]
    orchestrator = _orchestrator(session)
    orchestrator.handle_inbound(_message(ORDER_MESSAGE))

    result = orchestrator.handle_inbound(_message("9"))

    assert "número" in result.reply  # type: ignore[operator]
    need = session.scalar(select(SourcingNeed))
    assert need.supplier_id is None
    assert session.scalar(select(SupplierPurchaseOrder)) is None
