"""Guided (scripted) order-creation flow tests.

Drives the scripted sequence the session reset starts: client → product →
quantity → "¿querés agregar otro producto?" → finalize. The guided handler is
deterministic (FakeResponder raises if the LLM path ever runs) and the
finalize step shares the free-form draft persistence path, so the quote is
the multi-line ``_draft_quote_reply`` and "aprobá" confirms through the normal
DISPATCH machinery. Happy path runs through the real orchestrator (reset
routing included); disambiguation, pick lists and error paths call handlers
directly with ``InboundMessage`` + ``RoutingDecision`` + ``ConversationState``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.commands import GUIDED_ASK_CLIENT, GUIDED_ASK_MORE, GUIDED_ASK_PRODUCT
from src.agents.customer import SourcingDeps
from src.agents.dispatch import build_dispatch_handler
from src.agents.guided import _parse_quantity, _yes_no_answer, build_guided_handler
from src.agents.product_search import ProductEntry, ProductSearchResult, ProductSource
from src.channels.base import InboundMessage
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    Supplier,
)
from src.integrations.sheets import SheetsWriteStatus
from src.orchestrator.router import AgentName, RoutingDecision
from src.orchestrator.session import ConversationState
from src.pipeline import build_orchestrator
from src.supplier.searcher import FakeSupplierCatalogSearcher
from tests.test_customer import FakeProductSearcher


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
    """The guided flow must stay deterministic; raises if the LLM path runs."""

    def respond(self, messages):
        raise AssertionError("the guided flow must not call the responder")


class FakeSheets:
    """Append-only Sheets stand-in: records rows, always APPENDED."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []

    def append_order_row(
        self, order_id, *, customer_name=None, total=None, items_summary="", registered_at=None
    ):
        self.rows.append((order_id, items_summary))
        return SheetsWriteStatus.APPENDED


def _entry(sku: str, name: str) -> ProductEntry:
    return ProductEntry(sku=sku, name=name, source=ProductSource.LOCAL)


@pytest.fixture
def shop(db_session):
    """Two local products (priced from cost × margin), one customer, stock."""
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
            nombre_oficial="Amoladora recta",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=[],
        )
    )
    db_session.add(
        Catalogo(
            id=2,
            codigo_interno="LOCAL-2",
            supplier_id=1,
            nombre_oficial="Taladro",
            costo_proveedor=Decimal("200.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("270.00"),
            stock_disponible=5,
            sinonimos=[],
        )
    )
    db_session.add(Inventory(sku_id="LOCAL-1", quantity_on_hand=10))
    db_session.add(Inventory(sku_id="LOCAL-2", quantity_on_hand=5))
    db_session.flush()
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


def _searcher() -> FakeProductSearcher:
    return FakeProductSearcher(
        by_query={
            "amoladora": ProductSearchResult(
                source=ProductSource.LOCAL, entries=(_entry("LOCAL-1", "Amoladora recta"),)
            ),
            "taladro": ProductSearchResult(
                source=ProductSource.LOCAL, entries=(_entry("LOCAL-2", "Taladro"),)
            ),
            "brocas": ProductSearchResult(
                source=ProductSource.LOCAL,
                entries=(_entry("LOCAL-1", "Amoladora recta"), _entry("LOCAL-2", "Taladro")),
            ),
        }
    )


def _guided_deps(session):
    return SourcingDeps(session_factory=lambda: session, searcher=FakeSupplierCatalogSearcher())


def _guided_handler(session, searcher=None):
    return build_guided_handler(_guided_deps(session), searcher=searcher or _searcher())


def _decision() -> RoutingDecision:
    return RoutingDecision(agent=AgentName.GUIDED)


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id="owner", text=text)


def _client_step() -> ConversationState:
    """The state the session reset seeds: waiting for the client name."""
    return ConversationState(sender_id="owner", guided_step="ask_client")


# ------------------------------------------------------- pure answer parsers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("si", True),
        ("Sí", True),
        ("dale", True),
        ("dale!", True),
        ("ok", True),
        ("no", False),
        ("listo", False),
        (" listo ", False),
        ("nada más", False),
        ("no, nada más", False),
        ("ya está", False),
        ("eso es todo", False),
        ("quilombo", None),
        ("", None),
    ],
)
def test_yes_no_answer_variants(text, expected):
    """Pragmatic sí/no matching: accept common variants, reject anything else."""
    assert _yes_no_answer(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3", 3),
        (" 2 ", 2),
        ("quiero 10", 10),
        ("dame 4 unidades", 4),
        ("5 u", 5),
        ("0", None),
        ("-1", None),
        ("tres", None),
        ("3 taladros", None),
        ("", None),
    ],
)
def test_parse_quantity_variants(text, expected):
    """Only a bare positive integer answer (with an optional verb prefix) parses."""
    assert _parse_quantity(text) == expected


# ------------------------------------------------------------ guided handler


def test_guided_happy_path_all_five_steps(shop):
    """Client → product → quantity → no → quote → 'aprobá' confirms via Dispatch."""
    session = shop
    handler = _guided_handler(session)
    state = _client_step()

    outcome = handler(_message("Customer One"), state, _decision())
    assert outcome.reply == GUIDED_ASK_PRODUCT
    assert outcome.state.customer_id == 1
    assert outcome.state.guided_step == "ask_product"

    outcome = handler(_message("amoladora"), outcome.state, _decision())
    assert "¿Cuántas unidades de Amoladora recta" in outcome.reply
    assert outcome.state.guided_product is not None
    assert outcome.state.guided_product.sku == "LOCAL-1"

    outcome = handler(_message("2"), outcome.state, _decision())
    assert GUIDED_ASK_MORE in outcome.reply
    assert outcome.state.draft_items == ((_entry("LOCAL-1", "Amoladora recta"), 2),)
    assert outcome.state.guided_step == "ask_more"

    outcome = handler(_message("no"), outcome.state, _decision())
    assert outcome.state is not None
    assert outcome.state.awaiting_decision is True
    assert outcome.state.guided_step is None
    assert outcome.state.draft_items == ()
    assert "Pedido #1 para Customer One:" in outcome.reply
    assert "2 × Amoladora recta — 270.00 ARS" in outcome.reply
    assert "Total: 270.00 ARS" in outcome.reply
    assert "Respondé 'aprobá' o 'rechazá'." in outcome.reply
    order = session.scalar(select(Order))
    assert order is not None
    assert order.estado is OrderEstado.DRAFT

    # The owner confirms through the EXISTING dispatch machinery.
    dispatch = build_dispatch_handler(lambda: session, FakeSheets())
    confirmed = dispatch(
        _message("aprobá"),
        outcome.state,
        RoutingDecision(agent=AgentName.DISPATCH),
    )
    assert "confirmado" in confirmed.reply
    order = session.scalar(select(Order))
    assert order.estado is OrderEstado.CONFIRMED
    stock = session.scalar(select(Inventory).where(Inventory.sku_id == "LOCAL-1"))
    assert stock.quantity_on_hand == 8  # 2 units deducted at confirm


def test_guided_happy_path_through_orchestrator_routing(shop):
    """Reset → question 1 arrives via routing; every scripted turn reaches GUIDED."""
    session = shop
    sheets = FakeSheets()
    orchestrator = build_orchestrator(
        responder=FakeResponder(),
        searcher=_searcher(),
        sourcing=_guided_deps(session),
        dispatch=build_dispatch_handler(lambda: session, sheets),
    )

    result = orchestrator.handle_inbound(_message("Hola Bob"))
    assert result.reply == GUIDED_ASK_CLIENT
    assert result.state.guided_step == "ask_client"

    result = orchestrator.handle_inbound(_message("Customer One"))
    assert result.decision.agent is AgentName.GUIDED
    assert result.reply == GUIDED_ASK_PRODUCT

    result = orchestrator.handle_inbound(_message("taladro"))
    assert "¿Cuántas unidades de Taladro" in result.reply

    result = orchestrator.handle_inbound(_message("1"))
    assert GUIDED_ASK_MORE in result.reply

    result = orchestrator.handle_inbound(_message("listo"))
    assert result.state.awaiting_decision is True
    assert result.state.guided_step is None
    assert "Total: 270.00 ARS" in result.reply

    result = orchestrator.handle_inbound(_message("aprobá"))
    assert result.decision.agent is AgentName.DISPATCH
    assert "confirmado" in result.reply
    assert sheets.rows == [(1, "1 × LOCAL-2")]


def test_guided_ambiguous_client_shows_menu_and_pick_advances(shop):
    """An ambiguous client name renders the numbered menu; the pick advances."""
    session = shop
    session.add(
        Cliente(
            customer_id=2,
            nombre_comercial="Customer Two",
            telefono_norm="+5491166667777",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    session.commit()
    handler = _guided_handler(session)

    menu = handler(_message("Customer"), _client_step(), _decision())
    assert menu.state.customer_candidates is not None
    assert len(menu.state.customer_candidates) == 2
    assert "1) Customer One" in menu.reply
    assert "2) Customer Two" in menu.reply
    assert menu.state.guided_step == "ask_client"

    picked = handler(_message("2"), menu.state, _decision())
    assert picked.reply == GUIDED_ASK_PRODUCT
    assert picked.state.customer_id == 2
    assert picked.state.customer_candidates == ()


def test_guided_unknown_client_reports_and_stays(shop):
    """An unknown client name reports the miss without leaving the step."""
    outcome = _guided_handler(shop)(_message("Fantasma"), _client_step(), _decision())
    assert "No encontré ningún cliente" in outcome.reply
    assert outcome.state.guided_step == "ask_client"
    assert outcome.state.customer_id is None


def test_guided_product_search_multiple_hits_shows_numbered_list(shop):
    """Several hits render one product per line, numbered; the pick asks quantity."""
    session = shop
    handler = _guided_handler(session)
    state = ConversationState(sender_id="owner", guided_step="ask_product", customer_id=1)

    listed = handler(_message("brocas"), state, _decision())
    assert "1. Amoladora recta" in listed.reply
    assert "2. Taladro" in listed.reply
    assert listed.state.guided_product_options == (
        _entry("LOCAL-1", "Amoladora recta"),
        _entry("LOCAL-2", "Taladro"),
    )
    assert listed.state.guided_step == "ask_product"

    picked = handler(_message("2"), listed.state, _decision())
    assert "¿Cuántas unidades de Taladro" in picked.reply
    assert picked.state.guided_product.sku == "LOCAL-2"
    assert picked.state.guided_product_options == ()


def test_guided_product_search_no_hits_reasks(shop):
    """Zero hits say so and stay on the product question."""
    session = shop
    handler = _guided_handler(session)
    state = ConversationState(sender_id="owner", guided_step="ask_product", customer_id=1)

    outcome = handler(_message("cualquiercosa"), state, _decision())

    assert "No encontré" in outcome.reply
    assert outcome.state.guided_step == "ask_product"
    assert outcome.state.guided_product_options == ()


def test_guided_invalid_quantity_reasks_with_hint(shop):
    """A non-numeric or zero quantity is re-asked with a hint, no draft change."""
    session = shop
    handler = _guided_handler(session)
    state = ConversationState(
        sender_id="owner",
        guided_step="ask_quantity",
        customer_id=1,
        guided_product=_entry("LOCAL-1", "Amoladora recta"),
    )

    outcome = handler(_message("un montón"), state, _decision())

    assert "cantidad mayor a 0" in outcome.reply
    assert outcome.state.draft_items == ()
    assert outcome.state.guided_step == "ask_quantity"


def test_guided_more_yes_loops_back_to_product(shop):
    """'sí' loops back to the product question and a second product accumulates."""
    session = shop
    handler = _guided_handler(session)
    state = _client_step()

    state = handler(_message("Customer One"), state, _decision()).state
    state = handler(_message("amoladora"), state, _decision()).state
    state = handler(_message("1"), state, _decision()).state
    state = handler(_message("sí"), state, _decision()).state
    assert state.guided_step == "ask_product"

    state = handler(_message("taladro"), state, _decision()).state
    state = handler(_message("2"), state, _decision()).state
    assert state.draft_items == (
        (_entry("LOCAL-1", "Amoladora recta"), 1),
        (_entry("LOCAL-2", "Taladro"), 2),
    )

    outcome = handler(_message("no"), state, _decision())
    assert "1 × Amoladora recta — 135.00 ARS" in outcome.reply
    assert "2 × Taladro — 540.00 ARS" in outcome.reply
    assert "Total: 675.00 ARS" in outcome.reply
