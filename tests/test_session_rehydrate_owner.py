"""Owner-keyed conversation rehydration tests (task 4.3).

The in-memory conversation store expires after 30 minutes; the owner's
conversation is rebuilt from the database as the LATEST OPEN ORDER ACROSS ALL
CUSTOMERS (there is no owner entity). These tests prove ``rehydrate_conversation``
restores the latest open order, that an explicit ``pedido #N`` reference
(``order_ref``) overrides the latest, that Case A / Case B flags are restored,
and that the store falls back to the rehydrator on a miss or TTL expiry.
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
    SourcingNeed,
    SourcingState,
    Supplier,
)
from src.orchestrator.session import (
    ConversationState,
    ConversationStore,
    rehydrate_conversation,
)
from src.supplier.searcher import FakeSupplierCatalogSearcher, SupplierCandidate

OWNER_SENDER = "+5491100000000"


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


def _client(session, customer_id: int, name: str) -> None:
    session.add(
        Cliente(
            customer_id=customer_id,
            nombre_comercial=name,
            telefono_norm=f"+5491{customer_id:08d}",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )


@pytest.fixture
def shop(db_session):
    """Two customers and TWO open orders (one per customer) plus a Case B need."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    _client(db_session, 1, "Ferretería Don Juan")
    _client(db_session, 2, "Ferretería El Zorro")
    db_session.add(
        Supplier(id=1, code="SUP", business_name="Supplier X", default_margin_pct=Decimal(0))
    )
    order_a = Order(
        customer_id=1,
        estado=OrderEstado.PENDING_APPROVAL,
        sourcing_state=SourcingState.PENDING_ASSEMBLY,
    )
    db_session.add(order_a)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order_a.order_id,
            sku="CLV-001",
            cantidad=10,
            base_price=Decimal(100),
            final_price=Decimal(100),
            adjustment=Decimal(0),
        )
    )
    order_b = Order(
        customer_id=2,
        estado=OrderEstado.PENDING_APPROVAL,
        sourcing_state=SourcingState.IN_PREPARATION,
    )
    db_session.add(order_b)
    db_session.flush()
    db_session.add(SourcingNeed(order_id=order_b.order_id, sku="CLV-001", missing_quantity=6))
    db_session.flush()
    return {"session": db_session, "order_a": order_a, "order_b": order_b}


CANDIDATES = (
    SupplierCandidate(
        supplier_id=1,
        business_name="Supplier X",
        sku="CLV-001",
        description="Clavos Paris 2 Pulgadas",
        available_quantity=50,
    ),
)


def test_rehydrate_picks_latest_open_order_across_customers(shop):
    """El pedido abierto MÁS RECIENTE (de cualquier cliente) es la conversación activa."""
    session = shop["session"]
    state = rehydrate_conversation(session, OWNER_SENDER)
    assert state is not None
    # order_b was created last → it wins, even though it belongs to another customer.
    assert state.order_id == shop["order_b"].order_id
    assert state.customer_id == 2
    assert state.sourcing_selection_pending is True  # Case B selection restored
    assert state.sourcing_needs[0].sku == "CLV-001"


def test_rehydrate_restores_case_a_awaiting_decision(shop):
    """Un pedido Case A pendiente de aprobación restaura awaiting_decision."""
    session = shop["session"]
    state = rehydrate_conversation(
        session, OWNER_SENDER, searcher=FakeSupplierCatalogSearcher(CANDIDATES)
    )
    assert state is not None
    # order_b is IN_PREPARATION → selection pending; order_a must be reachable
    # via an explicit reference (the latest-open rule picks order_b).
    state_a = rehydrate_conversation(session, OWNER_SENDER, order_ref=shop["order_a"].order_id)
    assert state_a is not None
    assert state_a.order_id == shop["order_a"].order_id
    assert state_a.awaiting_decision is True
    assert state_a.sourcing_selection_pending is False


def test_rehydrate_order_ref_overrides_latest(shop):
    """La referencia 'pedido #N' rehidrata ESE pedido, no el más reciente."""
    session = shop["session"]
    state = rehydrate_conversation(session, OWNER_SENDER, order_ref=shop["order_a"].order_id)
    assert state is not None
    assert state.order_id == shop["order_a"].order_id
    assert state.customer_id == 1


def test_rehydrate_skips_rejected_orders(shop):
    """Un pedido rechazado no se rehidrata; el abierto más reciente gana."""
    session = shop["session"]
    session.add(
        Order(customer_id=1, estado=OrderEstado.REJECTED, sourcing_state=SourcingState.CANCELLED)
    )
    session.flush()
    state = rehydrate_conversation(session, OWNER_SENDER)
    assert state is not None
    assert state.order_id == shop["order_b"].order_id  # still the IN_PREPARATION one


def test_rehydrate_order_ref_to_rejected_order_returns_none(shop):
    """Una referencia explícita a un pedido rechazado no rehidrata estado."""
    session = shop["session"]
    rejected = Order(
        customer_id=1, estado=OrderEstado.REJECTED, sourcing_state=SourcingState.CANCELLED
    )
    session.add(rejected)
    session.flush()
    assert rehydrate_conversation(session, OWNER_SENDER, order_ref=rejected.order_id) is None


def test_rehydrate_no_open_orders_returns_none(db_session):
    """Sin pedidos abiertos no hay estado rehidratable."""
    assert rehydrate_conversation(db_session, OWNER_SENDER) is None


def test_store_miss_falls_back_to_rehydrator(shop):
    """Cuando la entrada en memoria expiró, el store la reconstruye de la DB."""
    session = shop["session"]
    searcher = FakeSupplierCatalogSearcher(CANDIDATES)
    store = ConversationStore(
        rehydrator=lambda sid: rehydrate_conversation(session, sid, searcher=searcher)
    )
    state = store.get(OWNER_SENDER)
    assert state is not None
    assert state.order_id == shop["order_b"].order_id
    assert state.sourcing_selection_pending is True
    # The rebuilt state is cached for subsequent turns.
    assert store.get(OWNER_SENDER).order_id == shop["order_b"].order_id


def test_store_rehydrates_after_ttl_expiry(shop):
    """Tras expirar el TTL, la conversación sobrevive vía rehidratación."""
    session = shop["session"]
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    store = ConversationStore(
        now=lambda: now,
        rehydrator=lambda sid: rehydrate_conversation(
            session, sid, searcher=FakeSupplierCatalogSearcher(CANDIDATES)
        ),
    )
    stale = ConversationState(
        sender_id=OWNER_SENDER, order_id=shop["order_b"].order_id, sourcing_selection_pending=True
    )
    stale.updated_at = now - timedelta(minutes=31)
    store.put(stale)
    state = store.get(OWNER_SENDER)
    assert state is not None
    assert state.sourcing_needs[0].sku == "CLV-001"
    assert state.sourcing_selection_pending is True
