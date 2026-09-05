"""Case B guided-loop e2e tests (owner pivot).

Drives the FULL guided Case B loop through the real orchestrator: a guided
order (session reset → client → product → quantity → finalize) is confirmed
with "aprobá", the confirm ceremony classifies Case B (LOCAL stock gap with
supplier candidates) and hands the selection prompt back, and the owner's
NUMBERED reply routes to the SOURCING agent, which accumulates the selection
into one OPEN PO per supplier; re-selection before execution moves the need
between POs. The guided flow is the only order path: no parse step involved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.agents.commands import GUIDED_ASK_CLIENT, GUIDED_ASK_MORE
from src.agents.customer import SourcingDeps
from src.agents.dispatch import build_dispatch_handler
from src.agents.guided import build_guided_handler
from src.agents.inventory import seed_inventory
from src.agents.product_search import ProductEntry, ProductSearchResult, ProductSource
from src.channels.base import InboundMessage
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Order,
    OrderEstado,
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
    """The guided flow must stay deterministic; raises if the LLM path runs."""

    def respond(self, messages):
        raise AssertionError("the guided flow must not call the LLM responder")


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
            nombre_comercial=CUSTOMER_NAME,
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
    # The guided handler owns a session per turn (closing it between turns),
    # so the seed must be committed to stay visible across turns.
    db_session.commit()
    return {"session": db_session}


def _product_searcher() -> object:
    """Product-query seam for the guided product step: one LOCAL hit."""

    class _Searcher:
        def search(self, query: str) -> ProductSearchResult:
            return ProductSearchResult(
                source=ProductSource.LOCAL,
                entries=(
                    ProductEntry(
                        sku="CLV-PRS-2",
                        name="Clavos Paris 2 Pulgadas (50mm)",
                        source=ProductSource.LOCAL,
                    ),
                ),
            )

    return _Searcher()


def _orchestrator(session) -> Orchestrator:
    deps = SourcingDeps(
        session_factory=lambda: session,
        searcher=FakeSupplierCatalogSearcher(CANDIDATES),
    )
    store = ConversationStore(
        rehydrator=lambda sid: rehydrate_conversation(session, sid, searcher=deps.searcher)
    )
    orchestrator = Orchestrator(store)
    orchestrator.register(
        AgentName.GUIDED,
        build_guided_handler(deps, searcher=_product_searcher()),  # type: ignore[arg-type]
    )
    orchestrator.register(
        AgentName.DISPATCH,
        build_dispatch_handler(
            lambda: session,
            _FakeSheets(),
            searcher=deps.searcher,
        ),
    )
    orchestrator.register(AgentName.SOURCING, build_sourcing_handler(lambda: session))
    return orchestrator


class _FakeSheets:
    """Append-only Sheets stand-in: records rows, always APPENDED."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []

    def append_order_row(
        self, order_id, *, customer_name=None, total=None, items_summary="", registered_at=None
    ):
        self.rows.append((order_id, items_summary))
        from src.integrations.sheets import SheetsWriteStatus

        return SheetsWriteStatus.APPENDED


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel="whatsapp", sender_id=OWNER_SENDER, text=text)


def _guided_order(session) -> Orchestrator:
    """Run the full guided flow up to (and including) the Case B prompt."""
    orchestrator = _orchestrator(session)
    result = orchestrator.handle_inbound(_message("Hola Bob"))
    assert result.reply == GUIDED_ASK_CLIENT
    orchestrator.handle_inbound(_message(CUSTOMER_NAME))
    result = orchestrator.handle_inbound(_message("clavos"))
    assert "¿Cuántas unidades" in (result.reply or "")
    result = orchestrator.handle_inbound(_message("10"))
    assert GUIDED_ASK_MORE in (result.reply or "")
    result = orchestrator.handle_inbound(_message("listo"))
    assert result.state is not None and result.state.awaiting_decision is True
    result = orchestrator.handle_inbound(_message("aprobá"))
    # The confirm ceremony discovered the LOCAL stock gap and handed the
    # selection prompt back: numbered supplier options in chat.
    assert "faltan 6" in (result.reply or "")
    assert "1) Supplier X" in (result.reply or "")
    assert "2) Supplier Y" in (result.reply or "")
    state = orchestrator.store.get(OWNER_SENDER)
    assert state is not None
    assert state.sourcing_selection_pending is True
    assert {c.supplier_id for c in state.sourcing_candidates} == {1, 2}
    return orchestrator


def test_guided_order_confirm_lists_missing_items_and_suppliers(shop):
    """A guided order confirmed into a stock gap shows the numbered suppliers."""
    session = shop["session"]
    _guided_order(session)

    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order is not None
    assert order.estado is OrderEstado.CONFIRMED  # the confirm ceremony confirmed it
    assert order.sourcing_state is SourcingState.PENDING_ASSEMBLY  # IN_PREPARATION on selection
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need is not None
    assert need.sku == "CLV-PRS-2"
    assert need.missing_quantity == 6
    assert need.supplier_id is None  # pending selection


def test_guided_case_b_owner_selection_accumulates_open_po(shop):
    """The owner's numbered reply drives confirm_selection: needs → OPEN PO."""
    session = shop["session"]
    orchestrator = _guided_order(session)

    result = orchestrator.handle_inbound(_message("1"))

    assert result.decision.agent is AgentName.SOURCING
    assert "PO #1" in (result.reply or "")
    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order is not None
    assert order.estado is OrderEstado.CONFIRMED
    assert order.sourcing_state is SourcingState.IN_PREPARATION
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need is not None
    assert need.supplier_id == 1

    po = session.scalar(select(SupplierPurchaseOrder).where(SupplierPurchaseOrder.supplier_id == 1))
    assert po is not None
    assert po.estado is SupplierPurchaseOrderState.OPEN
    item = session.scalar(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == po.po_id)
    )
    assert item is not None
    assert item.sku == "CLV-PRS-2"
    assert item.quantity == 6
    # The selection phase stays open until the PO is executed (re-selection).
    state = orchestrator.store.get(OWNER_SENDER)
    assert state is not None and state.sourcing_selection_pending is True


def test_guided_case_b_reselection_before_execution_moves_need_between_pos(shop):
    """Re-selecting a supplier before execution moves the need between POs."""
    session = shop["session"]
    orchestrator = _guided_order(session)
    orchestrator.handle_inbound(_message("1"))  # supplier X first

    result = orchestrator.handle_inbound(_message("2"))  # owner changes to Y

    assert "PO #2" in (result.reply or "")
    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order is not None
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need is not None
    assert need.supplier_id == 2
    # The old X PO lost the line (no double ordering).
    old_items = session.scalars(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == 1)
    ).all()
    assert old_items == []
    new_item = session.scalar(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == 2)
    )
    assert new_item is not None
    assert new_item.quantity == 6
    assert need.po_item_id == new_item.po_item_id


def test_guided_case_b_selection_survives_ttl_in_orchestrator_flow(shop):
    """Tras el TTL, la selección continúa rehidratada desde la DB."""
    session = shop["session"]
    orchestrator = _guided_order(session)

    # Expire the in-memory state; the store rehydrates from the DB.
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    state = orchestrator.store.get(OWNER_SENDER)
    assert state is not None
    state.updated_at = now - timedelta(minutes=31)
    orchestrator.store.put(state)

    result = orchestrator.handle_inbound(_message("2"))

    assert result.decision.agent is AgentName.SOURCING
    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order is not None
    need = session.scalar(select(SourcingNeed).where(SourcingNeed.order_id == order.order_id))
    assert need is not None
    assert need.supplier_id == 2


def test_guided_case_b_invalid_selection_number_asks_again(shop):
    """Un número fuera de rango no acumula y pide repetir."""
    session = shop["session"]
    orchestrator = _guided_order(session)

    result = orchestrator.handle_inbound(_message("9"))

    assert "número" in (result.reply or "")
    need = session.scalar(select(SourcingNeed))
    assert need is not None
    assert need.supplier_id is None
    assert session.scalar(select(SupplierPurchaseOrder)) is None
