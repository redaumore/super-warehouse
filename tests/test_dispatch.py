"""Dispatch agent tests (task 2.7).

Unit (no DB): owner notification through a fake notifier, decision parsing
(approve / reject / adjustments / unknown), and message formatting.

Integration (Postgres, skipped when down): applying the decision end-to-end —
approval with a per-line adjustment re-prices the order_items rows, and
rejection releases the reservations so the stock is available again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.dispatch import (
    Decision,
    DecisionAction,
    LineAdjustment,
    UnknownAdjustmentTargetError,
    UnknownDecisionError,
    apply_decision,
    format_quote_message,
    notify_owner,
    parse_decision,
)
from src.agents.inventory import available_stock, reserve_stock
from src.agents.sales import ItemInput, quote_order
from src.config import get_settings
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
from src.order_lifecycle.state import RequiresRequoteError


class FakeNotifier:
    """Records outbound sends; never touches the network."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, recipient, text):
        self.sent.append((recipient, text))


def _quote() -> object:
    return quote_order(
        (
            ItemInput(sku="CLV-001", cantidad=10, base_price=Decimal("100.00"), description="Clavos Paris 2 Pulgadas"),
            ItemInput(sku="TRN-002", cantidad=5, base_price=Decimal("50.00"), description="Tornillos M6 x 30"),
        ),
        None,
        None,
    )


# ---------------------------------------------------------------- unit tests


def test_notify_owner_sends_quote_via_notifier():
    """Se notifica al dueño la cotización a través del notificador."""
    notifier = FakeNotifier()
    notify_owner(notifier, "+5491100000000", _quote(), order_id=7, customer_name="Don Juan")
    assert len(notifier.sent) == 1
    recipient, body = notifier.sent[0]
    assert recipient == "+5491100000000"
    assert "Pedido #7 (Don Juan)" in body
    assert "aprobá" in body


def test_format_quote_message_mentions_lines_and_total():
    """El mensaje de cotización menciona las líneas y el total."""
    body = format_quote_message(_quote(), order_id=7)
    assert "1250.00" in body  # 10 × 100 + 5 × 50
    assert "Clavos Paris 2 Pulgadas" in body


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("sí, aprobá", DecisionAction.APPROVE),
        ("aprobá", DecisionAction.APPROVE),
        ("dale", DecisionAction.APPROVE),
        ("ok, dale para adelante", DecisionAction.APPROVE),
        ("no, rechazá", DecisionAction.REJECT),
        ("rechazá el pedido", DecisionAction.REJECT),
        ("no", DecisionAction.REJECT),
        ("hablamos mañana", DecisionAction.UNKNOWN),
        ("", DecisionAction.UNKNOWN),
    ],
)
def test_parse_decision_actions(text, action):
    """El texto del dueño se interpreta como aprobar, rechazar o desconocido."""
    assert parse_decision(text).action is action


def test_parse_decision_with_adjustment():
    """Aprobar con descuento extra por línea se interpreta como aprobación + ajuste.

    Spec: 'aprobá pero hacé un 5% de descuento extra en clavos' → approve + 5%.
    """
    decision = parse_decision("aprobá pero hacé un 5% de descuento extra en clavos")
    assert decision.action is DecisionAction.APPROVE
    assert decision.adjustments == (
        LineAdjustment(sku="clavos", extra_discount_pct=Decimal("0.05")),
    )


def test_parse_decision_accepts_decimal_percent():
    """Se aceptan porcentajes decimales en el ajuste."""
    decision = parse_decision("aprobá, 2,5% de descuento en tornillos")
    assert decision.action is DecisionAction.APPROVE
    assert decision.adjustments[0].extra_discount_pct == Decimal("0.025")


def test_parse_decision_reject_ignores_adjustment_mention():
    """Un rechazo ignora cualquier mención de ajuste."""
    decision = parse_decision("no, rechazá, no me sirve")
    assert decision.action is DecisionAction.REJECT
    assert decision.adjustments == ()


# -------------------------------------------------- integration (apply_decision)

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
def order_ctx(db_session):
    """Seed product (10 units), customer, PENDING_APPROVAL order and one item."""
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
            codigo_interno="CLV-001",
            proveedor_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=["clavos 2 pulgadas"],
        )
    )
    order = Order(customer_id=1, estado=OrderEstado.PENDING_APPROVAL, needs_requote=False)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="CLV-001",
            cantidad=10,
            base_price=Decimal("100.00"),
            final_price=Decimal("100.00"),
            adjustment=Decimal(0),
        )
    )
    db_session.flush()
    return {"session": db_session, "order": order, "sku": "CLV-001"}


def test_apply_approve_with_adjustment_reprises_line(order_ctx):
    """Aprobar con ajuste reprecifica la línea afectada.

    Spec: approval with '5% extra en clavos' re-prices the affected item.
    """
    decision = Decision(
        action=DecisionAction.APPROVE,
        adjustments=(LineAdjustment(sku="clavos", extra_discount_pct=Decimal("0.05")),),
    )
    apply_decision(order_ctx["session"], order_ctx["order"], decision, quote=_quote())
    assert order_ctx["order"].estado is OrderEstado.APPROVED
    item = order_ctx["session"].scalar(
        select(OrderItem).where(OrderItem.order_id == order_ctx["order"].order_id)
    )
    assert item.final_price == Decimal("95.00")  # 100 × 0.95
    assert item.adjustment == Decimal("5.00")


def test_apply_plain_approve_keeps_prices(order_ctx):
    """Aprobar sin cambios conserva los precios cotizados.

    Spec: plain approval keeps the previously quoted prices unchanged.
    """
    apply_decision(order_ctx["session"], order_ctx["order"], Decision(DecisionAction.APPROVE))
    assert order_ctx["order"].estado is OrderEstado.APPROVED
    item = order_ctx["session"].scalar(
        select(OrderItem).where(OrderItem.order_id == order_ctx["order"].order_id)
    )
    assert item.final_price == Decimal("100.00")
    assert item.adjustment == Decimal(0)


def test_apply_reject_releases_reservations(order_ctx):
    """Rechazar libera las reservas y el stock vuelve a estar disponible.

    Spec: rejection releases reservations — stock available to others again.
    """
    reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 6
    apply_decision(order_ctx["session"], order_ctx["order"], Decision(DecisionAction.REJECT))
    assert order_ctx["order"].estado is OrderEstado.REJECTED
    reservations = order_ctx["session"].scalars(
        select(StockReservation).where(
            StockReservation.order_id == order_ctx["order"].order_id
        )
    ).all()
    assert all(r.estado is ReservationEstado.RELEASED for r in reservations)
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10


def test_apply_approve_on_expired_reservation_requires_requote(order_ctx):
    """Aprobar sobre una reserva vencida exige recotizar."""
    reservation = reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    reservation.timestamp = datetime.now(UTC) - timedelta(minutes=31)
    order_ctx["session"].flush()
    with pytest.raises(RequiresRequoteError):
        apply_decision(order_ctx["session"], order_ctx["order"], Decision(DecisionAction.APPROVE))
    assert order_ctx["order"].estado is OrderEstado.PENDING_APPROVAL
    assert order_ctx["order"].needs_requote is True


def test_apply_unknown_decision_raises(order_ctx):
    """Aplicar una decisión desconocida lanza error."""
    with pytest.raises(UnknownDecisionError):
        apply_decision(order_ctx["session"], order_ctx["order"], Decision(DecisionAction.UNKNOWN))


def test_apply_adjustment_no_matching_quote_line_raises(order_ctx):
    """Un ajuste que nombra un producto fuera de la cotización no se puede aplicar.

    An adjustment naming a product outside the quote cannot be applied.
    """
    decision = Decision(
        action=DecisionAction.APPROVE,
        adjustments=(LineAdjustment(sku="pintura", extra_discount_pct=Decimal("0.05")),),
    )
    with pytest.raises(UnknownAdjustmentTargetError):
        apply_decision(order_ctx["session"], order_ctx["order"], decision, quote=_quote())
    assert order_ctx["order"].estado is OrderEstado.PENDING_APPROVAL


def test_apply_adjustment_sku_not_in_order_raises(order_ctx):
    """Sin cotización, un SKU desconocido en el ajuste se rechaza.

    Without a quote the target is a SKU; an unknown SKU is refused.
    """
    decision = Decision(
        action=DecisionAction.APPROVE,
        adjustments=(LineAdjustment(sku="ZZZ-999", extra_discount_pct=Decimal("0.05")),),
    )
    with pytest.raises(UnknownAdjustmentTargetError):
        apply_decision(order_ctx["session"], order_ctx["order"], decision)
    assert order_ctx["order"].estado is OrderEstado.PENDING_APPROVAL
