"""Conversation rehydration tests (task 3.5).

The in-memory conversation store expires after 30 minutes; the sourcing
selection must survive that TTL because the database is the source of truth.
These tests prove ``rehydrate_conversation`` rebuilds a sender's state from the
latest open Order + SourcingNeed rows (including recomputed supplier
candidates), and that the store falls back to the rehydrator on a miss.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from src.config import get_settings
from src.db.models import (
    Cliente,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    Proveedor,
    SourcingNeed,
    SourcingState,
)
from src.orchestrator.session import (
    ConversationState,
    ConversationStore,
    rehydrate_conversation,
)
from src.supplier.searcher import FakeSupplierCatalogSearcher, SupplierCandidate


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
    """Customer, two suppliers and a Case B order with one pending need."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial="Ferretería Don Juan",
            telefono_norm="+5491155551234",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(Proveedor(proveedor_id=1, razon_social="Proveedor X", margen_predeterminado=Decimal(0)))
    db_session.add(Proveedor(proveedor_id=2, razon_social="Proveedor Y", margen_predeterminado=Decimal(0)))
    order = Order(
        customer_id=1,
        estado=OrderEstado.PENDING_APPROVAL,
        sourcing_state=SourcingState.IN_PREPARATION,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(OrderItem(order_id=order.order_id, sku="CLV-001", cantidad=10, base_price=Decimal(100), final_price=Decimal(100), adjustment=Decimal(0)))
    db_session.add(SourcingNeed(order_id=order.order_id, sku="CLV-001", missing_quantity=6))
    db_session.flush()
    return {"session": db_session, "order": order}


CANDIDATES = (
    SupplierCandidate(supplier_id=1, business_name="Proveedor X", sku="CLV-001", description="Clavos Paris 2 Pulgadas", available_quantity=50),
    SupplierCandidate(supplier_id=2, business_name="Proveedor Y", sku="CLV-001", description="Clavos Paris 2 Pulgadas", available_quantity=30),
)


def test_rehydrate_restores_case_b_selection_from_db(shop):
    """La selección de proveedor pendiente se reconstruye desde la DB."""
    state = rehydrate_conversation(
        shop["session"], "+5491155551234", searcher=FakeSupplierCatalogSearcher(CANDIDATES)
    )
    assert state is not None
    assert state.order_id == shop["order"].order_id
    assert state.customer_id == 1
    assert state.sourcing_selection_pending is True
    assert len(state.sourcing_needs) == 1
    assert state.sourcing_needs[0].sku == "CLV-001"
    assert state.sourcing_needs[0].missing_quantity == 6
    assert state.sourcing_needs[0].supplier_id is None
    # Candidates recomputed through the searcher for the missing SKU.
    assert {c.supplier_id for c in state.sourcing_candidates} == {1, 2}


def test_rehydrate_restores_case_a_awaiting_decision(shop):
    """Un pedido Case A pendiente de aprobación restaura awaiting_decision."""
    session = shop["session"]
    order = Order(customer_id=1, estado=OrderEstado.PENDING_APPROVAL, sourcing_state=SourcingState.PENDING_ASSEMBLY)
    session.add(order)
    session.flush()
    session.add(OrderItem(order_id=order.order_id, sku="TRN-002", cantidad=2, base_price=Decimal(50), final_price=Decimal(50), adjustment=Decimal(0)))
    session.flush()
    state = rehydrate_conversation(session, "+5491155551234")
    assert state is not None
    assert state.order_id == order.order_id  # latest non-rejected order wins
    assert state.awaiting_decision is True
    assert state.sourcing_selection_pending is False


def test_rehydrate_skips_rejected_orders(shop):
    """Un pedido rechazado/cancelado no se rehidrata como conversación activa."""
    session = shop["session"]
    session.add(Order(customer_id=1, estado=OrderEstado.REJECTED, sourcing_state=SourcingState.CANCELLED))
    session.flush()
    state = rehydrate_conversation(session, "+5491155551234")
    assert state is not None
    assert state.order_id == shop["order"].order_id  # still the IN_PREPARATION one


def test_rehydrate_unknown_sender_returns_none(shop):
    """Un sender sin cliente registrado no tiene estado rehidratable."""
    state = rehydrate_conversation(shop["session"], "+5491199990000")
    assert state is None


def test_store_miss_falls_back_to_rehydrator(shop):
    """Cuando la entrada en memoria expiró, el store la reconstruye de la DB."""
    session = shop["session"]
    searcher = FakeSupplierCatalogSearcher(CANDIDATES)
    store = ConversationStore(
        rehydrator=lambda sid: rehydrate_conversation(session, sid, searcher=searcher)
    )
    state = store.get("+5491155551234")
    assert state is not None
    assert state.order_id == shop["order"].order_id
    assert state.sourcing_selection_pending is True
    # The rebuilt state is cached for subsequent turns.
    assert store.get("+5491155551234").order_id == shop["order"].order_id


def test_store_rehydrates_after_ttl_expiry(shop):
    """Tras expirar el TTL, la selección sobrevive vía rehidratación."""
    session = shop["session"]
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    store = ConversationStore(
        now=lambda: now,
        rehydrator=lambda sid: rehydrate_conversation(session, sid, searcher=FakeSupplierCatalogSearcher(CANDIDATES)),
    )
    stale = ConversationState(
        sender_id="+5491155551234", order_id=shop["order"].order_id, sourcing_selection_pending=True
    )
    stale.updated_at = now - timedelta(minutes=31)
    store.put(stale)
    state = store.get("+5491155551234")
    assert state is not None
    assert state.sourcing_needs[0].sku == "CLV-001"
    assert state.sourcing_selection_pending is True