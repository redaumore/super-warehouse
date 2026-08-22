"""E2E order flow (task 4.7): WhatsApp intake → quote → owner approval → stock.

Drives the real pipeline end-to-end with the WhatsApp adapter (outbound sends
mocked at the httpx boundary) and the real Postgres fixture: an inbound text
order resolves the customer, disambiguates the item, soft-locks stock, quotes,
notifies the owner through WhatsApp, and — on the owner's "aprobá" — converts
the reservation, registers in Sheets, deducts stock and confirms.

Skipped cleanly when Postgres is not running.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.customer import lookup_phone
from src.agents.dispatch import Decision, DecisionAction, apply_decision, format_quote_message
from src.agents.disambiguation import resolve_item
from src.agents.inventory import available_stock, reserve_stock
from src.agents.sales import ItemInput, quote_order
from src.channels.whatsapp import WhatsAppChannel
from src.config import Settings, get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    Proveedor,
    ReservationEstado,
    StockReservation,
)
from src.integrations.sheets import SheetsWriter
from src.orchestrator.approval import register_approved_order

CONFIGURED = Settings(
    whatsapp_token="tok", whatsapp_phone_id="123456", whatsapp_verify_token="verifyme"
)

OWNER_PHONE = "+5491100000000"
CUSTOMER_PHONE = "+5491155551234"

TEXT_ORDER_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {"from": "5491155551234", "text": {"body": "clavos de 2 pulgadas"}}
                        ]
                    }
                }
            ]
        }
    ]
}


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
                "TRUNCATE order_items, orders, stock_reservations, catalogo, proveedores, "
                "clientes, lista_precios RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def shop(db_session):
    """Seed the catalog, price list, customer and supplier for the flow."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Proveedor(
            proveedor_id=1,
            razon_social="Proveedor Test",
            margen_predeterminado=Decimal(0),
        )
    )
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
        Catalogo(
            id=1,
            codigo_interno="CLV-PRS-2",
            proveedor_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=50,
            sinonimos=["clavo paris 2", "clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    return {"session": db_session, "sku": "CLV-PRS-2"}


def _mock_whatsapp_send() -> MagicMock:
    """Patch the channel's httpx client so sends are recorded, not posted."""
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    response = MagicMock()
    response.raise_for_status.return_value = None
    client.post.return_value = response
    return client


class AsyncNotifier:
    """Bridges the sync Notifier protocol to the async WhatsApp channel.

    The sync agent pipeline hands off to the async channel edge by scheduling
    coroutines; the test awaits them (``await notifier.drain()``) before
    asserting on the mocked httpx transport.
    """

    def __init__(self, channel: WhatsAppChannel) -> None:
        self.channel = channel
        self.tasks: list[asyncio.Task] = []

    def send_text(self, recipient: str, text: str) -> None:
        self.tasks.append(
            asyncio.get_running_loop().create_task(self.channel.send_text(recipient, text))
        )

    async def drain(self) -> None:
        await asyncio.gather(*self.tasks)
        self.tasks = []


async def _send_whatsapp_message(session, shop):
    """Run the intake→quote pipeline and return the created order."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    message = await channel.parse_inbound(TEXT_ORDER_PAYLOAD)
    assert message.text == "clavos de 2 pulgadas"

    # 1. Customer & context
    phone = lookup_phone(session, message.sender_id)
    assert phone.status.value == "KNOWN"

    # 2. Disambiguation → inventory (soft-lock) → quote
    resolution = resolve_item(session, message.text)
    assert resolution.kind.value == "AUTO_MAPPED"
    assert resolution.candidate.sku == shop["sku"]
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
        None,
        None,
    )

    # 3. Order row + owner notification through WhatsApp
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

    client = _mock_whatsapp_send()
    notifier = AsyncNotifier(channel)
    with patch("src.channels.whatsapp.httpx.AsyncClient", return_value=client):
        notifier.send_text(
            OWNER_PHONE, format_quote_message(quote, order.order_id, "Ferretería Don Juan")
        )
        await notifier.drain()
    assert client.post.await_count == 1
    body = client.post.await_args.kwargs["json"]
    assert body["text"]["body"].startswith(f"Pedido #{order.order_id}")
    return order


async def test_e2e_text_order_flows_to_owner_approval_and_stock_deduction(shop):
    """Un pedido de texto llega, cotiza al dueño y al aprobar descuenta stock."""
    session = shop["session"]
    order = await _send_whatsapp_message(session, shop)

    # Owner approves "aprobá" → convert reservation + Sheets + deduct + confirm.
    decision = Decision(action=DecisionAction.APPROVE)
    apply_decision(session, order, decision)
    notifier_client = _mock_whatsapp_send()
    notifier = AsyncNotifier(WhatsAppChannel(settings=CONFIGURED))
    with patch("src.channels.whatsapp.httpx.AsyncClient", return_value=notifier_client):
        result = register_approved_order(
            session,
            order,
            sheets=SheetsWriter(gc=None, settings=Settings(google_sheets_credentials_file="")),
            notifier=notifier,
            owner_phone=OWNER_PHONE,
            customer_name="Ferretería Don Juan",
        )
        await notifier.drain()
    assert result.order.estado is OrderEstado.APPROVED
    reservation = session.scalar(
        select(StockReservation).where(StockReservation.order_id == order.order_id)
    )
    assert reservation.estado is ReservationEstado.CONVERTED
    product = session.get(Catalogo, 1)
    assert product.stock_disponible == 40  # 50 − 10 reserved
    assert available_stock(session, shop["sku"]) == 40
    assert notifier_client.post.await_count == 1
    confirm = notifier_client.post.await_args.kwargs["json"]["text"]["body"]
    assert "aprobado" in confirm
    assert "cuarentena" in confirm  # Sheets not configured → quarantined, not blocked


async def test_e2e_owner_reject_releases_reservation(shop):
    """Al rechazar el pedido, la reserva se libera y el stock vuelve a estar libre."""
    session = shop["session"]
    order = await _send_whatsapp_message(session, shop)
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
                                    "from": "5491155551234",
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


async def test_e2e_http_error_on_confirm_still_completes_flow(shop):
    """Aunque el envío de confirmación falle, el flujo de aprobación no se corta."""
    session = shop["session"]
    order = await _send_whatsapp_message(session, shop)
    apply_decision(session, order, Decision(action=DecisionAction.APPROVE))
    failing_client = AsyncMock()
    failing_client.__aenter__.return_value = failing_client
    failing_client.__aexit__.return_value = False
    response = MagicMock()
    response.raise_for_status.side_effect = httpx.HTTPError("graph down")
    failing_client.post.return_value = response
    notifier = AsyncNotifier(WhatsAppChannel(settings=CONFIGURED))
    with patch("src.channels.whatsapp.httpx.AsyncClient", return_value=failing_client):
        register_approved_order(
            session,
            order,
            sheets=SheetsWriter(gc=None, settings=Settings(google_sheets_credentials_file="")),
            notifier=notifier,
            owner_phone=OWNER_PHONE,
        )
        with pytest.raises(Exception):
            await notifier.drain()
    # Registration side effects already committed to the session before the send.
    assert order.estado is OrderEstado.APPROVED
    assert session.get(Catalogo, 1).stock_disponible == 40