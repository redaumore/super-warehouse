"""Sourcing need persistence tests (task 5.4).

The multi-turn Case B selection must survive the in-memory 30-minute TTL: the
missing items and the owner's supplier choice live on ``SourcingNeed`` rows and
``rehydrate_conversation`` rebuilds the conversation from them. These tests
prove the persistence layer and the TTL-survival round trip.
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
    SourcingNeed,
    SourcingState,
    Supplier,
)
from src.orchestrator.session import (
    ConversationState,
    ConversationStore,
    rehydrate_conversation,
)
from src.sourcing.persistence import (
    record_supplier_selection,
    sourcing_needs_for_order,
    upsert_sourcing_need,
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
def order_ctx(db_session):
    """A Case B order (IN_PREPARATION) with one pending need."""
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
    db_session.add(
        Supplier(id=1, code="SUP", business_name="Supplier X", default_margin_pct=Decimal(0))
    )
    db_session.add(
        Supplier(id=2, code="SUY", business_name="Supplier Y", default_margin_pct=Decimal(0))
    )
    order = Order(
        customer_id=1,
        estado=OrderEstado.PENDING_APPROVAL,
        sourcing_state=SourcingState.IN_PREPARATION,
    )
    db_session.add(order)
    db_session.flush()
    return {"session": db_session, "order": order}


CANDIDATES = (
    SupplierCandidate(
        supplier_id=1,
        business_name="Supplier X",
        sku="CLV-001",
        description="Clavos Paris 2 Pulgadas",
        available_quantity=50,
    ),
    SupplierCandidate(
        supplier_id=2,
        business_name="Supplier Y",
        sku="CLV-001",
        description="Clavos Paris 2 Pulgadas",
        available_quantity=30,
    ),
)


def test_upsert_creates_and_updates_need(order_ctx):
    """upsert_sourcing_need crea y actualiza la necesidad sin duplicar."""
    session = order_ctx["session"]
    order_id = order_ctx["order"].order_id
    first = upsert_sourcing_need(session, order_id, "CLV-001", 6)
    second = upsert_sourcing_need(session, order_id, "CLV-001", 9)
    assert second.need_id == first.need_id
    assert second.missing_quantity == 9
    assert len(sourcing_needs_for_order(session, order_id)) == 1


def test_record_supplier_selection_updates_in_place(order_ctx):
    """The supplier selection persists and can be re-chosen before execution."""
    session = order_ctx["session"]
    need = upsert_sourcing_need(session, order_ctx["order"].order_id, "CLV-001", 6)
    record_supplier_selection(session, need.need_id, 1)
    assert session.get(SourcingNeed, need.need_id).supplier_id == 1
    record_supplier_selection(session, need.need_id, 2)
    assert session.get(SourcingNeed, need.need_id).supplier_id == 2


def test_record_selection_unknown_need_raises(order_ctx):
    """Seleccionar sobre una necesidad inexistente se rechaza."""
    with pytest.raises(KeyError, match="unknown sourcing need"):
        record_supplier_selection(order_ctx["session"], 999, 1)


def test_selection_survives_ttl_via_db_rehydration(order_ctx):
    """La selección parcial sobrevive el TTL: se reconstruye desde la DB."""
    session = order_ctx["session"]
    upsert_sourcing_need(session, order_ctx["order"].order_id, "CLV-001", 6)
    other = upsert_sourcing_need(session, order_ctx["order"].order_id, "PINT-001", 2)

    # Owner picked supplier 1 for the first item; the state expires later.
    record_supplier_selection(session, other.need_id, 1)
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    store = ConversationStore(
        now=lambda: now,
        rehydrator=lambda sid: rehydrate_conversation(
            session, sid, searcher=FakeSupplierCatalogSearcher(CANDIDATES)
        ),
    )
    stale = ConversationState(
        sender_id="+5491155551234",
        order_id=order_ctx["order"].order_id,
        sourcing_selection_pending=True,
    )
    stale.updated_at = now - timedelta(minutes=31)
    store.put(stale)

    state = store.get("+5491155551234")
    assert state is not None
    assert state.order_id == order_ctx["order"].order_id
    assert state.sourcing_selection_pending is True  # one need still unassigned
    needs = {n.sku: n for n in state.sourcing_needs}
    assert len(needs) == 2
    # The persisted selection is recovered (DB source of truth).
    assert needs["PINT-001"].supplier_id == 1
    assert needs["CLV-001"].supplier_id is None
