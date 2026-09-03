"""Integration tests for finalizing a product-selection draft."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.customer import SourcingDeps, build_handler
from src.agents.product_search import ProductEntry, ProductSource
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
    Supplier,
    StockReservation,
)
from src.orchestrator.router import AgentName, RoutingDecision
from src.orchestrator.session import ConversationState
from src.supplier.searcher import FakeSupplierCatalogSearcher


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


class FakeResponder:
    """The finalize path must persist directly without calling the LLM."""

    def respond(self, messages):
        raise AssertionError("finalizing a draft must not call the responder")


def _entry(
    sku: str,
    name: str,
    *,
    source: ProductSource,
    price: Decimal | None = None,
    currency: str | None = None,
    codigo_proveedor: str | None = None,
) -> ProductEntry:
    return ProductEntry(
        sku=sku,
        name=name,
        source=source,
        price=price,
        currency=currency,
        codigo_proveedor=codigo_proveedor,
    )


@pytest.fixture
def shop(db_session):
    """Seed the Base list, one customer, a local product, and stock."""
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
    db_session.add(Supplier(id=1, code="SUP", business_name="Supplier", default_margin_pct=Decimal(0)))
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="LOCAL-1",
            supplier_id=1,
            nombre_oficial="Local item",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("999.00"),
            stock_disponible=10,
            sinonimos=[],
        )
    )
    db_session.add(Inventory(sku_id="LOCAL-1", quantity_on_hand=10))
    db_session.flush()
    # The fixture inserts explicit ids, which does not advance the sequences;
    # bump them so subsequent auto-id inserts do not collide.
    for table, column in (
        ("clientes", "customer_id"),
        ("suppliers", "id"),
        ("catalogo", "id"),
        ("lista_precios", "lista_id"),
    ):
        db_session.execute(
            text(f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), 1, true)")
        )
    db_session.commit()
    return db_session


def _handler(session):
    deps = SourcingDeps(
        session_factory=lambda: session,
        searcher=FakeSupplierCatalogSearcher(),
    )
    return build_handler(FakeResponder(), sourcing=deps)


def _decision() -> RoutingDecision:
    return RoutingDecision(agent=AgentName.CUSTOMER)


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id="owner", text=text)


def test_finalize_local_draft_uses_cost_margin_and_clears_draft(shop):
    """A local draft is priced from catalog cost, reserved, and persisted."""
    state = ConversationState(
        sender_id="owner",
        draft_items=((_entry("LOCAL-1", "Local item", source=ProductSource.LOCAL), 2),),
    )

    outcome = _handler(shop)(_message("cerrá el pedido para Customer One"), state, _decision())

    assert outcome.state is not None
    assert outcome.state.draft_items == ()
    assert outcome.state.awaiting_decision is True
    order = shop.scalar(select(Order))
    assert order is not None
    assert order.estado is OrderEstado.PENDING_APPROVAL
    assert order.subtotal == Decimal("270.00")
    assert order.total == Decimal("270.00")
    item = shop.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("135.00")
    assert shop.scalar(select(StockReservation).where(StockReservation.order_id == order.order_id))


def test_finalize_rag_without_rate_saves_pending_snapshot(shop):
    """A non-ARS RAG draft is saved with its original price and pending totals."""
    state = ConversationState(
        sender_id="owner",
        draft_items=(
            (
                _entry(
                    "AMX-1",
                    "RAG item",
                    source=ProductSource.RAG,
                    price=Decimal("10.00"),
                    currency="USD",
                    codigo_proveedor="AMX",
                ),
                1,
            ),
        ),
    )

    outcome = _handler(shop)(_message("cerrá el pedido para Customer One"), state, _decision())

    assert outcome.state is not None
    order = shop.scalar(select(Order))
    assert order is not None
    assert order.conversion_pending is True
    assert order.subtotal is None
    assert order.total is None
    item = shop.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.source == "RAG"
    assert item.moneda == "USD"
    assert item.precio_original == Decimal("10.0000")
    assert shop.scalar(select(StockReservation).where(StockReservation.order_id == order.order_id)) is None


def test_finalize_unknown_customer_then_create_attaches_waiting_draft(shop):
    """The minimal create command attaches the existing draft after a name miss."""
    state = ConversationState(
        sender_id="owner",
        draft_items=((_entry("LOCAL-1", "Local item", source=ProductSource.LOCAL), 1),),
    )
    handler = _handler(shop)

    waiting = handler(_message("cerrá el pedido para New Customer"), state, _decision())
    assert waiting.state is not None
    assert waiting.state.draft_items == state.draft_items
    assert shop.scalar(select(Order)) is None

    finished = handler(
        _message("nuevo cliente New Customer +5491166667777"), waiting.state, _decision()
    )

    assert finished.state is not None
    assert finished.state.draft_items == (), finished.reply
    order = shop.scalar(select(Order))
    assert order is not None
    client = shop.scalar(select(Cliente).where(Cliente.nombre_comercial == "New Customer"))
    assert client is not None
    assert order.customer_id == client.customer_id
    assert client.lista_precios_id == 1


def test_finalize_ambiguous_customer_keeps_menu_and_draft(shop):
    """An ambiguous customer name waits for a numbered choice without persisting."""
    shop.add(
        Cliente(
            customer_id=2,
            nombre_comercial="Customer Two",
            telefono_norm="+5491166667777",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    shop.flush()
    state = ConversationState(
        sender_id="owner",
        draft_items=((_entry("LOCAL-1", "Local item", source=ProductSource.LOCAL), 1),),
    )

    outcome = _handler(shop)(_message("cerrá el pedido para Customer"), state, _decision())

    assert outcome.state is not None
    assert outcome.state.customer_disambiguation_pending is True
    assert len(outcome.state.customer_candidates) == 2
    assert outcome.state.draft_items == state.draft_items
    assert shop.scalar(select(Order)) is None
