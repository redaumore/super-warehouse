"""Approval orchestration tests (task 3.4).

Unit (no DB): order totals and items summaries over ORM-shaped objects.

Integration (Postgres, skipped when down): the full APPROVE flow — state
transition, reservation conversion, stock deduction, Sheets append and owner
confirmation — plus the failure paths (stale order refuses, Sheets quarantine
never blocks the flow).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.dispatch import Decision, DecisionAction, LineAdjustment, apply_decision
from src.agents.inventory import reserve_stock, seed_inventory
from src.agents.sales import ItemInput, quote_order
from src.config import Settings, get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    Proveedor,
    ReservationEstado,
    StockReservation,
)
from src.integrations.sheets import SheetsWriter, SheetsWriteStatus
from src.orchestrator.approval import (
    approve_and_register,
    build_items_summary,
    order_total,
    register_approved_order,
)
from src.order_lifecycle.state import RequiresRequoteError


class FakeNotifier:
    """Records outbound sends; never touches the network."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, recipient, text):
        self.sent.append((recipient, text))


# ---------------------------------------------------------------- unit tests


def test_order_total_sums_final_price_times_quantity():
    """El total del pedido suma precio final por cantidad, redondeado a centavos."""
    order = SimpleNamespace(
        items=[
            SimpleNamespace(sku="A", final_price=Decimal("100.00"), cantidad=2),
            SimpleNamespace(sku="B", final_price=Decimal("50.50"), cantidad=3),
        ]
    )
    assert order_total(order) == Decimal("351.50")


def test_order_total_with_adjusted_line():
    """Una línea ajustada aporta su precio final rebajado al total."""
    order = SimpleNamespace(
        items=[SimpleNamespace(sku="A", final_price=Decimal("95.00"), cantidad=1)]
    )
    assert order_total(order) == Decimal("95.00")


def test_build_items_summary_lists_each_line():
    """El resumen de ítems lista cantidad por SKU separado por punto y coma."""
    order = SimpleNamespace(
        items=[
            SimpleNamespace(sku="CLV-001", cantidad=10),
            SimpleNamespace(sku="TRN-002", cantidad=5),
        ]
    )
    assert build_items_summary(order) == "10 × CLV-001; 5 × TRN-002"


# -------------------------------------------------- integration (approval flow)

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
                "catalogo, proveedores, clientes, lista_precios RESTART IDENTITY CASCADE"
            )
        )


def _on_hand(session, sku: str) -> int:
    """Read a SKU's canonical on-hand quantity from Inventory."""
    row = session.scalar(select(Inventory).where(Inventory.sku_id == sku))
    assert row is not None
    return row.quantity_on_hand


@pytest.fixture
def order_ctx(db_session):
    """Seed product (10 units), customer, PENDING_APPROVAL order + one item."""
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
    db_session.flush()
    seed_inventory(db_session)
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


def _sheets_writer() -> SheetsWriter:
    # No credentials: append quarantines internally without touching the network.
    return SheetsWriter(gc=None, settings=Settings(google_sheets_credentials_file=""))


def test_approve_and_register_converts_deducts_and_confirms(order_ctx):
    """Aprobar registra: convierte reservas, descuenta stock, agrega a Sheets y confirma."""
    reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    notifier = FakeNotifier()
    result = approve_and_register(
        order_ctx["session"],
        order_ctx["order"],
        sheets=_sheets_writer(),
        notifier=notifier,
        owner_phone="+5491100000000",
        customer_name="Ferretería Don Juan",
    )
    assert result.order.estado is OrderEstado.APPROVED
    assert result.converted == 1
    assert result.total == Decimal("1000.00")
    reservations = order_ctx["session"].scalars(
        select(StockReservation).where(
            StockReservation.order_id == order_ctx["order"].order_id
        )
    ).all()
    assert all(r.estado is ReservationEstado.CONVERTED for r in reservations)
    assert _on_hand(order_ctx["session"], "CLV-001") == 6
    # Legacy catalogo stock counter is untouched by the approval deduction.
    assert order_ctx["session"].get(Catalogo, 1).stock_disponible == 10
    assert len(notifier.sent) == 1
    assert "Pedido #" in notifier.sent[0][1]
    assert "aprobado" in notifier.sent[0][1]


def test_register_after_adjustment_approve_uses_revised_total(order_ctx):
    """Registrar tras un ajuste usa el total reprecificado y confirma igual."""
    reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    quote = quote_order(
        (
            ItemInput(
                sku="CLV-001",
                cantidad=10,
                base_price=Decimal("100.00"),
                description="Clavos Paris 2 Pulgadas",
            ),
        ),
        None,
        None,
    )
    apply_decision(
        order_ctx["session"],
        order_ctx["order"],
        Decision(
            action=DecisionAction.APPROVE,
            adjustments=(LineAdjustment(sku="clavos", extra_discount_pct=Decimal("0.05")),),
        ),
        quote=quote,
    )
    notifier = FakeNotifier()
    result = register_approved_order(
        order_ctx["session"],
        order_ctx["order"],
        sheets=_sheets_writer(),
        notifier=notifier,
        owner_phone="+5491100000000",
        customer_name="Ferretería Don Juan",
    )
    assert result.total == Decimal("950.00")
    item = order_ctx["session"].scalar(
        select(OrderItem).where(OrderItem.order_id == order_ctx["order"].order_id)
    )
    assert item.final_price == Decimal("95.00")
    assert _on_hand(order_ctx["session"], "CLV-001") == 6
    assert notifier.sent[0][1].startswith("Pedido #")


def test_approve_on_expired_reservation_refuses_without_side_effects(order_ctx):
    """Aprobar una reserva vencida exige recotizar y no produce efectos laterales."""
    reservation = reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    reservation.timestamp = datetime.now(UTC) - timedelta(minutes=31)
    order_ctx["session"].flush()
    notifier = FakeNotifier()
    with pytest.raises(RequiresRequoteError):
        approve_and_register(
            order_ctx["session"],
            order_ctx["order"],
            sheets=_sheets_writer(),
            notifier=notifier,
            owner_phone="+5491100000000",
        )
    assert order_ctx["order"].estado is OrderEstado.PENDING_APPROVAL
    assert order_ctx["order"].needs_requote is True
    reservation = order_ctx["session"].get(StockReservation, reservation.reservation_id)
    assert reservation.estado is ReservationEstado.ACTIVE
    assert notifier.sent == []
    assert _on_hand(order_ctx["session"], "CLV-001") == 10  # nothing deducted


def test_sheets_quarantine_never_blocks_approval(order_ctx):
    """La cuarentena de Sheets no bloquea: se confirma y el estado lo reporta."""
    reserve_stock(
        order_ctx["session"],
        order_ctx["sku"],
        customer_id=1,
        cantidad=4,
        order_id=order_ctx["order"].order_id,
    )
    notifier = FakeNotifier()
    result = approve_and_register(
        order_ctx["session"],
        order_ctx["order"],
        sheets=_sheets_writer(),
        notifier=notifier,
        owner_phone="+5491100000000",
    )
    assert result.sheets_status is SheetsWriteStatus.QUARANTINED
    assert result.order.estado is OrderEstado.APPROVED
    assert _on_hand(order_ctx["session"], "CLV-001") == 6
    assert "cuarentena" in notifier.sent[0][1]