"""Integration tests for finalizing a product-selection draft."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from src.agents.customer import (
    ASK_CUSTOMER_FINALIZE_REPLY,
    EMPTY_DRAFT_FINALIZE_REPLY,
    SourcingDeps,
    build_handler,
)
from src.agents.product_search import ProductEntry, ProductSource
from src.backoffice.customer_orders import set_default_margin
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
    StockReservation,
    Supplier,
)
from src.integrations.rag import RagPrice
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
    db_session.add(
        Supplier(id=1, code="SUP", business_name="Supplier", default_margin_pct=Decimal(0))
    )
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
    assert order.estado is OrderEstado.DRAFT  # persisted at the first add that knows the customer
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
    assert (
        shop.scalar(select(StockReservation).where(StockReservation.order_id == order.order_id))
        is None
    )


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


def test_finalize_without_customer_name_asks_before_persisting(shop):
    """A name-less finalize asks for the customer and keeps the draft untouched."""
    state = ConversationState(
        sender_id="owner",
        draft_items=((_entry("LOCAL-1", "Local item", source=ProductSource.LOCAL), 1),),
    )

    outcome = _handler(shop)(_message("cerrá el pedido"), state, _decision())

    assert outcome.reply == ASK_CUSTOMER_FINALIZE_REPLY
    assert outcome.state is not None
    assert outcome.state.draft_items == state.draft_items
    assert outcome.state.customer_id is None
    assert shop.scalar(select(Order)) is None


def test_finalize_empty_draft_replies_deterministically_without_llm(shop):
    """A finalize command on an empty draft is refused, with no LLM and no order."""
    state = ConversationState(sender_id="owner")

    # FakeResponder raises if invoked, so reaching the assertion proves the
    # turn never fell through to the LLM chat route.
    outcome = _handler(shop)(_message("Cerra el pedido"), state, _decision())

    assert outcome.reply == EMPTY_DRAFT_FINALIZE_REPLY
    assert outcome.state is not None
    assert outcome.state.draft_items == ()
    assert outcome.state.customer_id is None
    assert shop.scalar(select(Order)) is None


def test_create_client_with_empty_draft_creates_client_without_order(shop):
    """nuevo cliente on an empty draft creates the client and persists no order."""
    state = ConversationState(sender_id="owner")

    outcome = _handler(shop)(
        _message("nuevo cliente Empty Draft Client +5491166667777"), state, _decision()
    )

    assert outcome.state is not None
    client = shop.scalar(select(Cliente).where(Cliente.nombre_comercial == "Empty Draft Client"))
    assert client is not None
    assert client.telefono_norm == "+5491166667777"
    assert client.lista_precios_id == 1
    assert shop.scalar(select(Order)) is None


def test_finalize_session_customer_persists_without_asking_name(shop):
    """A draft with state.customer_id set finalizes without any name request."""
    state = ConversationState(
        sender_id="owner",
        customer_id=1,
        draft_items=((_entry("LOCAL-1", "Local item", source=ProductSource.LOCAL), 1),),
    )

    outcome = _handler(shop)(_message("cerrá el pedido"), state, _decision())

    assert outcome.state is not None
    assert outcome.state.draft_items == ()
    assert outcome.state.customer_id == 1
    order = shop.scalar(select(Order))
    assert order is not None
    assert order.customer_id == 1
    assert order.subtotal == Decimal("135.00")
    assert order.total == Decimal("135.00")
    assert outcome.reply.startswith(f"Pedido #{order.order_id} para Customer One")


def test_finalize_rag_without_price_falls_back_to_endpoint_lookup(shop):
    """A price-less RAG draft line is priced through the rag client endpoint."""
    rag_client = Mock()
    rag_client.price_lookup.return_value = RagPrice(135.5, "ARS")
    deps = SourcingDeps(
        session_factory=lambda: shop,
        searcher=FakeSupplierCatalogSearcher(),
        rag_client=rag_client,
    )
    handler = build_handler(FakeResponder(), sourcing=deps)
    state = ConversationState(
        sender_id="owner",
        draft_items=(
            (_entry("AT-5044", "RAG item", source=ProductSource.RAG, codigo_proveedor="AMX"), 1),
        ),
    )

    outcome = handler(_message("cerrá el pedido para Customer One"), state, _decision())

    assert outcome.state is not None
    assert outcome.state.draft_items == ()
    rag_client.price_lookup.assert_called_once_with("AT-5044", "AMX")
    order = shop.scalar(select(Order))
    assert order is not None
    assert order.subtotal == Decimal("162.60")  # 135.5 × 1.20 default margin
    assert order.total == Decimal("162.60")
    item = shop.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.sku == "AT-5044"
    assert item.source == "RAG"
    assert item.base_price == Decimal("162.60")
    assert item.precio_original == Decimal("135.5000")
    assert (
        shop.scalar(select(StockReservation).where(StockReservation.order_id == order.order_id))
        is None
    )


def test_second_finalize_for_same_customer_is_refused(shop):
    """A second draft for a customer with one open is refused; the first survives."""
    state = ConversationState(
        sender_id="owner",
        draft_items=((_entry("LOCAL-1", "Local item", source=ProductSource.LOCAL), 1),),
    )
    handler = _handler(shop)
    first = handler(_message("cerrá el pedido para Customer One"), state, _decision())
    assert first.state is not None
    assert first.state.awaiting_decision is True
    assert shop.scalar(select(Order)).estado is OrderEstado.DRAFT

    second = handler(_message("cerrá el pedido para Customer One"), state, _decision())

    assert "ya tiene un pedido abierto" in second.reply  # type: ignore[operator]
    assert shop.scalar(select(func.count(Order.order_id))) == 1


def test_remove_command_deletes_persisted_draft_line(shop):
    """'sacá X' borra la OrderItem del Draft persistido y el pedido sigue DRAFT."""
    state = ConversationState(
        sender_id="owner",
        draft_items=((_entry("LOCAL-1", "Local item", source=ProductSource.LOCAL), 1),),
    )
    handler = _handler(shop)
    finished = handler(_message("cerrá el pedido para Customer One"), state, _decision())
    assert finished.state is not None
    order_id = finished.state.order_id
    assert order_id is not None

    outcome = handler(_message("sacá el Local item"), finished.state, _decision())

    assert "saqué" in outcome.reply  # type: ignore[operator]
    assert shop.scalar(select(func.count(OrderItem.id))) == 0  # line deleted
    order = shop.get(Order, order_id)
    assert order.estado is OrderEstado.DRAFT  # empty Draft stays DRAFT


def test_default_margin_edit_prices_subsequent_chat_finalize(shop):
    """set_default_margin(27.50) prices a NEW chat finalize for an unmapped supplier."""
    assert set_default_margin(shop, Decimal("27.50")) == Decimal("27.50")
    shop.commit()
    state = ConversationState(
        sender_id="owner",
        draft_items=(
            (
                _entry(
                    "AMX-1",
                    "RAG item",
                    source=ProductSource.RAG,
                    price=Decimal("100.00"),
                    currency="ARS",
                    codigo_proveedor="AMX",
                ),
                1,
            ),
        ),
    )

    outcome = _handler(shop)(_message("cerrá el pedido para Customer One"), state, _decision())

    assert outcome.state is not None
    assert outcome.state.draft_items == ()
    order = shop.scalar(select(Order))
    assert order is not None
    assert order.subtotal == Decimal("127.50")  # 100 × (1 + 27.50%)
    assert order.total == Decimal("127.50")
    item = shop.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("127.50")
