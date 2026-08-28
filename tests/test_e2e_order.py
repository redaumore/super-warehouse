"""E2E order flow (owner pivot): WhatsApp intake → quote → approval → stock.

Drives the real pipeline boundaries end-to-end with the WhatsApp adapter
(outbound sends mocked at the httpx boundary) and the real Postgres fixture:
the owner's inbound text order resolves the customer BY NAME, soft-locks stock,
quotes in chat, and — on the owner's "aprobá" — converts the reservation,
registers in Sheets, deducts stock and confirms with the in-chat confirmation
text. A Sheets failure rolls the approval back: the order stays PENDING.

Skipped cleanly when Postgres is not running.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.customers import resolve_customer_name
from src.agents.disambiguation import resolve_item
from src.agents.dispatch import Decision, DecisionAction, apply_decision
from src.agents.inventory import available_stock, reserve_stock, seed_inventory
from src.agents.sales import ItemInput, quote_order
from src.channels.whatsapp import WhatsAppChannel
from src.config import Settings, get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    Supplier,
    ReservationEstado,
    StockReservation,
)
from src.integrations.sheets import SheetsWriteStatus
from src.orchestrator.approval import (
    SheetsRegistrationError,
    register_approved_order,
)

CONFIGURED = Settings(
    whatsapp_token="tok", whatsapp_phone_id="123456", whatsapp_verify_token="verifyme"
)

OWNER_SENDER = "+5491100000000"
CUSTOMER_NAME = "Ferretería Don Juan"

TEXT_ORDER_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "from": "5491100000000",
                                "text": {
                                    "body": f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas"
                                },
                            }
                        ]
                    }
                }
            ]
        }
    ]
}


class FakeSheets:
    """Append-only Sheets stand-in with an injectable outcome."""

    def __init__(self, status: SheetsWriteStatus = SheetsWriteStatus.APPENDED) -> None:
        self.status = status
        self.rows: list[tuple[int, str]] = []

    def append_order_row(
        self, order_id, *, customer_name=None, total=None, items_summary="", registered_at=None
    ):
        self.rows.append((order_id, items_summary))
        return self.status


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
def _clean_schema(db_engine):
    yield
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE supplier_purchase_order_items, supplier_purchase_orders, "
                "sourcing_needs, inventory, order_items, orders, stock_reservations, "
                "catalogo, suppliers, clientes, lista_precios RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def shop(db_session):
    """Seed the catalog, price list, customer and supplier for the flow."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Supplier(
            supplier_id=1,
            razon_social="Supplier Test",
            margen_predeterminado=Decimal(0),
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
    return {"session": db_session, "sku": "CLV-PRS-2"}


async def _send_whatsapp_order(session, shop) -> Order:
    """Run intake → name resolution → quote and return the created order."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    message = await channel.parse_inbound(TEXT_ORDER_PAYLOAD)
    assert message.text == f"para {CUSTOMER_NAME} quiero 10 clavos de 2 pulgadas"

    # 1. Customer resolved by NAME (owner pivot — no sender-phone lookup).
    resolution = resolve_customer_name(session, "Ferretería Don Juan")
    assert resolution.kind.value == "EXACT"
    customer = resolution.candidate
    assert customer is not None

    # 2. Disambiguation → inventory (soft-lock) → quote.
    item_resolution = resolve_item(session, "clavos de 2 pulgadas")
    assert item_resolution.kind.value == "AUTO_MAPPED"
    assert item_resolution.candidate.sku == shop["sku"]
    reserve_stock(session, shop["sku"], customer_id=1, cantidad=10, ttl_minutes=30)
    assert available_stock(session, shop["sku"]) == 40
    quote = quote_order(
        (
            ItemInput(
                sku=shop["sku"],
                cantidad=10,
                base_price=Decimal("135.00"),
                description="Clavos Paris 2 Pulgadas (50mm)",
            ),
        ),
        customer.lista_precios.descuento_lista_pct,
        customer.descuento_particular_pct,
    )

    # 3. Order row + order items (the quote is the in-chat reply).
    order = Order(customer_id=1, estado=OrderEstado.PENDING_APPROVAL, needs_requote=False)
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            order_id=order.order_id,
            sku=shop["sku"],
            cantidad=10,
            base_price=Decimal("135.00"),
            final_price=quote.lines[0].final_price,
            adjustment=Decimal(0),
        )
    )
    session.flush()
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.sku == shop["sku"])
    )
    reservation.order_id = order.order_id
    session.flush()
    session.commit()
    return order


async def test_e2e_owner_order_approves_and_deducts_stock(shop):
    """El pedido del dueño se aprueba: reserva convertida, Sheets y stock descontado."""
    session = shop["session"]
    order = await _send_whatsapp_order(session, shop)

    apply_decision(session, order, Decision(action=DecisionAction.APPROVE))
    sheets = FakeSheets()
    result = register_approved_order(session, order, sheets=sheets)

    assert result.order.estado is OrderEstado.APPROVED
    assert "aprobado" in result.confirmation_text
    assert "Registrado en Google Sheets" in result.confirmation_text
    assert sheets.rows == [(order.order_id, "10 × CLV-PRS-2")]
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.CONVERTED
    # The approval deduction writes Inventory (canonical on-hand), not the
    # legacy catalogo counter.
    on_hand = session.scalar(select(Inventory).where(Inventory.sku_id == shop["sku"]))
    assert on_hand.quantity_on_hand == 40  # 50 − 10 reserved
    assert session.get(Catalogo, 1).stock_disponible == 50  # legacy counter untouched
    assert available_stock(session, shop["sku"]) == 40


async def test_e2e_sheets_failure_keeps_order_pending(shop):
    """Si Sheets falla, la aprobación se revierte y el pedido sigue pendiente."""
    session = shop["session"]
    order = await _send_whatsapp_order(session, shop)

    sheets = FakeSheets(status=SheetsWriteStatus.QUARANTINED)
    with pytest.raises(SheetsRegistrationError):
        register_approved_order(session, order, sheets=sheets)
    # The caller rolls back; a fresh read proves the order stayed PENDING and
    # the reservation stayed ACTIVE (the in-memory conversion was never flushed).
    session.expire_all()
    fresh = session.scalar(select(Order).where(Order.order_id == order.order_id))
    assert fresh.estado is OrderEstado.PENDING_APPROVAL
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.ACTIVE


async def test_e2e_owner_reject_releases_reservation(shop):
    """Al rechazar el pedido, la reserva se libera y el stock vuelve a estar libre."""
    session = shop["session"]
    order = await _send_whatsapp_order(session, shop)
    apply_decision(session, order, Decision(action=DecisionAction.REJECT))
    assert order.estado is OrderEstado.REJECTED
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.RELEASED
    assert available_stock(session, shop["sku"]) == 50


async def test_e2e_whatsapp_voice_payload_flags_media(shop):
    """Una nota de voz de WhatsApp se normaliza marcando media_type voice."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "5491100000000",
                                    "voice": {"id": "v1", "mime_type": "audio/ogg"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    message = await channel.parse_inbound(payload)
    assert message.media_type == "voice"
    assert message.raw["media_id"] == "v1"
