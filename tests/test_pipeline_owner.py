"""Pipeline E2E tests for the owner pivot (task 5.3).

Drives ``handle_inbound`` end-to-end with a fake channel and a real
orchestrator: the owner's message passes the gate, is parsed, the customer is
resolved by name, the Case A quote lands in the owner's chat, and the approval
reply registers the Sheets row. A non-owner sender is rejected at the edge with
a polite reply and never routes — no order is created.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.agents.customer import SourcingDeps
from src.agents.dispatch import build_dispatch_handler
from src.agents.intake import SimpleOrderParser
from src.agents.inventory import seed_inventory
from src.channels.base import InboundMessage
from src.config import Settings, get_settings
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
from src.orchestrator.owner import rejection_reply
from src.pipeline import build_orchestrator, handle_inbound
from src.supplier.searcher import FakeSupplierCatalogSearcher

OWNER_WHATSAPP = "+5491100000000"
OWNER_TELEGRAM = "123456789"
CUSTOMER_NAME = "Ferretería Don Juan"


class FakeChannel:
    """In-memory channel adapter recording outbound sends."""

    name = "whatsapp"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, sender_id: str, text: str) -> None:
        self.sent.append((sender_id, text))


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
    """Catalog with 50 units, customer, supplier; committed for the pipeline."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Supplier(
            id=1,
            code="TES",
            business_name="Test Supplier",
            default_margin_pct=Decimal(0),
        )
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
            stock_disponible=50,
            sinonimos=["clavo paris 2", "clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    seed_inventory(db_session)
    db_session.commit()
    return {"session": db_session}


@contextmanager
def _pipeline_patches(channel, orchestrator, settings):
    with (
        patch("src.pipeline.CHANNELS", {"whatsapp": channel, "telegram": channel}),
        patch("src.pipeline.ORCHESTRATOR", orchestrator),
        patch("src.pipeline.get_settings", return_value=settings),
    ):
        yield


def _settings() -> Settings:
    return Settings(
        owner_telegram_chat_id=OWNER_TELEGRAM,
        owner_whatsapp_phone=OWNER_WHATSAPP,
    )


def _orchestrator(session, sheets):
    deps = SourcingDeps(
        session_factory=lambda: session,
        searcher=FakeSupplierCatalogSearcher(),
    )
    return build_orchestrator(
        responder=FakeResponder(),
        sourcing=deps,
        parser=SimpleOrderParser(),
        dispatch=build_dispatch_handler(lambda: session, sheets),
    )


async def test_owner_turn_flows_to_case_a_quote_and_approval(shop):
    """El turno del dueño: gate → parse → resolver → cotizar en chat → aprobar → Sheets."""
    session = shop["session"]
    sheets = FakeSheets()
    channel = FakeChannel()
    orchestrator = _orchestrator(session, sheets)
    settings = _settings()

    with _pipeline_patches(channel, orchestrator, settings):
        await handle_inbound(
            InboundMessage(
                channel="whatsapp",
                sender_id=OWNER_WHATSAPP,
                text=f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas",
            )
        )

    # The quote is the owner's in-chat reply (no separate push).
    assert len(channel.sent) == 1
    sender, reply = channel.sent[0]
    assert sender == OWNER_WHATSAPP
    assert "Pedido #1 de Ferretería Don Juan confirmado" in reply
    assert "aprobá" in reply

    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order is not None
    assert order.estado is OrderEstado.PENDING_APPROVAL

    # The owner's approval reply routes to the wired DISPATCH and registers.
    with _pipeline_patches(channel, orchestrator, settings):
        await handle_inbound(
            InboundMessage(channel="whatsapp", sender_id=OWNER_WHATSAPP, text="aprobá")
        )

    assert len(channel.sent) == 2
    assert "aprobado" in channel.sent[1][1]
    assert sheets.rows == [(order.order_id, "10 × CLV-PRS-2")]
    on_hand = session.scalar(select(Inventory).where(Inventory.sku_id == "CLV-PRS-2"))
    assert on_hand.quantity_on_hand == 40
    order = session.scalar(select(Order).order_by(Order.order_id.desc()))
    assert order.estado is OrderEstado.APPROVED


async def test_non_owner_sender_rejected_before_routing(shop):
    """Un sender que no es el dueño recibe el rechazo cortés y no se enruta."""
    session = shop["session"]
    channel = FakeChannel()
    orchestrator = _orchestrator(session, FakeSheets())
    settings = _settings()

    with _pipeline_patches(channel, orchestrator, settings):
        await handle_inbound(
            InboundMessage(
                channel="whatsapp",
                sender_id="+5491133334444",
                text=f"para {CUSTOMER_NAME} quiero 10 clavos",
            )
        )

    assert channel.sent == [("+5491133334444", rejection_reply())]
    assert session.scalar(select(Order)) is None  # never routed, nothing created


async def test_telegram_owner_gate_accepts_configured_chat_id(shop):
    """El gate de Telegram acepta solo el chat id configurado del dueño."""
    session = shop["session"]
    channel = FakeChannel()
    orchestrator = _orchestrator(session, FakeSheets())
    settings = _settings()

    with _pipeline_patches(channel, orchestrator, settings):
        await handle_inbound(
            InboundMessage(
                channel="telegram",
                sender_id=OWNER_TELEGRAM,
                text=f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas",
            )
        )
        await handle_inbound(
            InboundMessage(channel="telegram", sender_id="987654321", text="quiero 10 clavos")
        )

    # The owner's order flowed; the impostor was rejected before routing.
    assert channel.sent[0][0] == OWNER_TELEGRAM
    assert "Pedido #1 de Ferretería Don Juan confirmado" in channel.sent[0][1]
    assert channel.sent[1] == ("987654321", rejection_reply())
    orders = session.scalars(select(Order)).all()
    assert len(orders) == 1  # only the owner's order exists
