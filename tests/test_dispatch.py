"""Dispatch agent tests.

Unit (no DB): decision parsing (confirm / cancel / adjustments / unknown),
``parse_order_reference`` (pedido #N) and message formatting.

Integration (Postgres, skipped when down): applying the decision end-to-end —
an APPROVE with a per-line adjustment re-prices the order_items rows (the
confirm ceremony runs the transition), and a REJECT cancels the order,
releasing the reservations so the stock is available again.
"""

from __future__ import annotations

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
    parse_decision,
    parse_order_reference,
)
from src.agents.inventory import available_stock, reserve_stock, seed_inventory
from src.agents.sales import ItemInput, Quote, quote_order
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    ReservationEstado,
    StockReservation,
    Supplier,
)


def _quote() -> Quote:
    return quote_order(
        (
            ItemInput(
                sku="CLV-001",
                cantidad=10,
                base_price=Decimal("100.00"),
                description="Clavos Paris 2 Pulgadas",
            ),
            ItemInput(
                sku="TRN-002",
                cantidad=5,
                base_price=Decimal("50.00"),
                description="Tornillos M6 x 30",
            ),
        ),
        None,
        None,
    )


# ---------------------------------------------------------------- unit tests


def test_format_quote_message_mentions_lines_and_total():
    """El mensaje de cotización menciona las líneas y el total."""
    body = format_quote_message(_quote(), order_id=7)
    assert "1250.00" in body  # 10 × 100 + 5 × 50
    assert "Clavos Paris 2 Pulgadas" in body


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("aprobá el pedido #3", 3),
        ("aprobá el pedido#3", 3),
        ("aprobá # 42", 42),
        ("aprobá", None),
        ("rechazá el pedido", None),
        ("", None),
        ("aprobá el pedido #3 y el #7", 3),  # first reference wins
    ],
)
def test_parse_order_reference(text, expected):
    """La referencia 'pedido #N' se extrae como número de pedido."""
    assert parse_order_reference(text) == expected


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
    """El texto del dueño se interpreta como confirmar, cancelar o desconocido."""
    assert parse_decision(text).action is action


def test_parse_decision_with_adjustment():
    """Aprobar con descuento extra por línea se interpreta como confirm + ajuste.

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
                "TRUNCATE supplier_purchase_order_items, supplier_purchase_orders, "
                "sourcing_needs, inventory, order_items, orders, stock_reservations, "
                "stock_adjustments, catalogo, suppliers, clientes, lista_precios "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def order_ctx(db_session):
    """Seed product (10 units), customer, DRAFT order and one item."""
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
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=["clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    seed_inventory(db_session)
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, needs_requote=False)
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
            source="LOCAL",
        )
    )
    db_session.flush()
    return {"session": db_session, "order": order, "sku": "CLV-001"}


def test_apply_approve_with_adjustment_reprises_line(order_ctx):
    """Confirmar con ajuste reprecifica la línea afectada.

    Spec: approval with '5% extra en clavos' re-prices the affected item; the
    ceremony (not apply_decision) runs the state transition.
    """
    decision = Decision(
        action=DecisionAction.APPROVE,
        adjustments=(LineAdjustment(sku="clavos", extra_discount_pct=Decimal("0.05")),),
    )
    apply_decision(order_ctx["session"], order_ctx["order"], decision, quote=_quote())
    assert order_ctx["order"].estado is OrderEstado.DRAFT  # still awaiting the ceremony
    item = order_ctx["session"].scalar(
        select(OrderItem).where(OrderItem.order_id == order_ctx["order"].order_id)
    )
    assert item.final_price == Decimal("95.00")  # 100 × 0.95
    assert item.adjustment == Decimal("5.00")


def test_apply_plain_approve_keeps_prices(order_ctx):
    """Confirmar sin cambios conserva los precios cotizados."""
    apply_decision(order_ctx["session"], order_ctx["order"], Decision(DecisionAction.APPROVE))
    assert order_ctx["order"].estado is OrderEstado.DRAFT  # ceremony owns the transition
    item = order_ctx["session"].scalar(
        select(OrderItem).where(OrderItem.order_id == order_ctx["order"].order_id)
    )
    assert item.final_price == Decimal("100.00")
    assert item.adjustment == Decimal(0)


def test_apply_reject_cancels_order_and_releases_reservations(order_ctx):
    """Rechazar cancela el pedido: reservas liberadas y stock disponible."""
    reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 6
    apply_decision(order_ctx["session"], order_ctx["order"], Decision(DecisionAction.REJECT))
    assert order_ctx["order"].estado is OrderEstado.CANCELED
    assert order_ctx["order"].rejected_at is not None
    reservations = (
        order_ctx["session"]
        .scalars(
            select(StockReservation).where(StockReservation.order_id == order_ctx["order"].order_id)
        )
        .all()
    )
    assert all(r.estado is ReservationEstado.RELEASED for r in reservations)
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10


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
    assert order_ctx["order"].estado is OrderEstado.DRAFT


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
    assert order_ctx["order"].estado is OrderEstado.DRAFT
