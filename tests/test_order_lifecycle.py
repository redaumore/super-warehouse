"""Order state machine tests (tasks 2.9 + 2.11 RED + Phase 4.3).

Unit tests (no DB): transition legality and the needs_requote guard, using a
minimal fake session so no Postgres is required.

Integration tests (Postgres, skipped when down) — the RED acceptance tests from
task 2.11:
- reject releases every reservation and the stock becomes available again;
- an order whose reservation expired cannot be approved silently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.inventory import available_stock, reserve_stock, seed_inventory
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Order,
    OrderEstado,
    Proveedor,
    ReservationEstado,
    StockReservation,
)
from src.order_lifecycle.state import (
    InvalidTransitionError,
    RequiresRequoteError,
    approve_order,
    expire_reservations,
    mark_dispatched,
    reject_order,
    requires_requote,
)


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


# ---------------------------------------------------------------- unit tests


class _FakeSession:
    """Minimal stand-in for the SQLAlchemy Session used by the transitions."""

    def __init__(self, *, stale_reservation: bool = False, stale_rows: list | None = None):
        self.stale = stale_reservation
        self.stale_rows = stale_rows or []
        self.executed: list = []
        self.flushed = 0

    def scalar(self, _statement):
        return 1 if self.stale else None

    def scalars(self, _statement):
        return _ScalarResult(list(self.stale_rows))

    def execute(self, statement):
        self.executed.append(statement)

    def flush(self):
        self.flushed += 1

    def add(self, _obj):
        pass


class _ScalarResult:
    """Duck-typed stand-in for SQLAlchemy's ScalarResult."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


def _pending_order(order_id: int = 1, *, needs_requote: bool = False) -> Order:
    return Order(
        order_id=order_id,
        customer_id=1,
        estado=OrderEstado.PENDING_APPROVAL,
        needs_requote=needs_requote,
    )


def test_approve_pending_order_moves_to_approved():
    """Aprobar un pedido pendiente lo mueve a Aprobado."""
    session = _FakeSession()
    order = _pending_order()
    approve_order(session, order)
    assert order.estado is OrderEstado.APPROVED
    assert order.approved_at is not None
    assert order.needs_requote is False


def test_approve_flagged_order_raises_requote():
    """El flag needs_requote bloquea la aprobación silenciosa.

    needs_requote flag blocks silent approval even without stale rows.
    """
    session = _FakeSession()
    order = _pending_order(needs_requote=True)
    with pytest.raises(RequiresRequoteError):
        approve_order(session, order)
    assert order.estado is OrderEstado.PENDING_APPROVAL


def test_approve_order_with_stale_reservation_raises_requote():
    """Un pedido con reserva vencida no se aprueba en silencio: exige recotizar.

    Expired order cannot be approved silently: stale ACTIVE reservation → raise.
    """
    session = _FakeSession(stale_reservation=True)
    order = _pending_order()
    with pytest.raises(RequiresRequoteError):
        approve_order(session, order)
    assert order.estado is OrderEstado.PENDING_APPROVAL
    assert order.needs_requote is True  # flagged for the re-quote


def test_approve_non_pending_order_is_invalid():
    """Aprobar un pedido que no está pendiente es una transición inválida."""
    approved = _pending_order()
    approved.estado = OrderEstado.APPROVED
    with pytest.raises(InvalidTransitionError, match="cannot approve"):
        approve_order(_FakeSession(), approved)

    rejected = _pending_order()
    rejected.estado = OrderEstado.REJECTED
    with pytest.raises(InvalidTransitionError, match="cannot approve"):
        approve_order(_FakeSession(), rejected)


def test_reject_pending_order_moves_to_rejected_and_releases():
    """Rechazar un pedido pendiente lo mueve a Rechazado y libera reservas."""
    session = _FakeSession()
    order = _pending_order()
    reject_order(session, order)
    assert order.estado is OrderEstado.REJECTED
    assert order.rejected_at is not None
    assert session.executed, "rejection must execute the reservation release"


def test_reject_non_pending_order_is_invalid():
    """Rechazar un pedido que no está pendiente es inválido."""
    approved = _pending_order()
    approved.estado = OrderEstado.APPROVED
    with pytest.raises(InvalidTransitionError, match="cannot reject"):
        reject_order(_FakeSession(), approved)


def test_mark_dispatched_only_from_approved():
    """Despachar solo es válido desde el estado Aprobado."""
    session = _FakeSession()
    approved = _pending_order()
    approved.estado = OrderEstado.APPROVED
    mark_dispatched(session, approved)
    assert approved.estado is OrderEstado.IN_DISPATCH

    pending = _pending_order()
    with pytest.raises(InvalidTransitionError, match="cannot dispatch"):
        mark_dispatched(session, pending)


def test_requires_requote_true_when_flagged():
    """El flag needs_requote hace que requiera recotizar."""
    order = _pending_order(needs_requote=True)
    assert requires_requote(_FakeSession(), order) is True


def test_requires_requote_true_when_stale_reservation():
    """Una reserva vencida hace que requiera recotizar."""
    order = _pending_order()
    assert requires_requote(_FakeSession(stale_reservation=True), order) is True


def test_requires_requote_false_when_clean():
    """Sin flag ni reservas vencidas, no requiere recotizar."""
    order = _pending_order()
    assert requires_requote(_FakeSession(), order) is False


def test_expire_reservations_flags_order_when_rows_expired():
    """Expirar reservas vencidas marca el pedido para recotizar."""
    stale = StockReservation(
        reservation_id=1,
        sku="CLV-001",
        customer_id=1,
        order_id=1,
        cantidad=3,
        ttl_minutes=30,
        estado=ReservationEstado.ACTIVE,
    )
    session = _FakeSession(stale_rows=[stale])
    order = _pending_order()
    count = expire_reservations(session, order)
    assert count == 1
    assert stale.estado is ReservationEstado.EXPIRED
    assert order.needs_requote is True


def test_expire_reservations_noop_when_nothing_expired():
    """Sin reservas vencidas, expirar no hace nada."""
    session = _FakeSession(stale_rows=[])
    order = _pending_order()
    assert expire_reservations(session, order) == 0
    assert order.needs_requote is False


# ------------------------------------------------------- integration (RED 2.11)

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


@pytest.fixture
def order_ctx(db_session):
    """Seed a product (10 units), a customer, and a PENDING_APPROVAL order."""
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
    return {"session": db_session, "order": order, "sku": "CLV-001"}


def _reserve_ctx(ctx, cantidad, *, minutes_ago: int | None = None):
    reservation = reserve_stock(
        ctx["session"], ctx["sku"], customer_id=1, cantidad=cantidad, order_id=ctx["order"].order_id
    )
    if minutes_ago is not None:
        reservation.timestamp = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        ctx["session"].flush()
    return reservation


def test_reject_releases_reservations_and_restores_stock(order_ctx):
    """Rechazar libera las reservas y restaura el stock disponible.

    RED (2.11): reject release — stock becomes available to other customers.
    """
    _reserve_ctx(order_ctx, 4)
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 6
    reject_order(order_ctx["session"], order_ctx["order"])
    assert order_ctx["order"].estado is OrderEstado.REJECTED
    reservations = order_ctx["session"].scalars(
        select(StockReservation).where(
            StockReservation.order_id == order_ctx["order"].order_id
        )
    ).all()
    assert all(r.estado is ReservationEstado.RELEASED for r in reservations)
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10


def test_expired_order_cannot_be_approved(order_ctx):
    """Un pedido con reserva vencida no se puede aprobar: exige recotizar.

    RED (2.11): expired order cannot be approved silently — re-quote required.
    """
    _reserve_ctx(order_ctx, 4, minutes_ago=31)
    with pytest.raises(RequiresRequoteError):
        approve_order(order_ctx["session"], order_ctx["order"])
    assert order_ctx["order"].estado is OrderEstado.PENDING_APPROVAL
    assert order_ctx["order"].needs_requote is True
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10  # expired lock freed


def test_fresh_reservation_can_be_approved(order_ctx):
    """Una reserva vigente no bloquea la aprobación.

    A non-expired reservation does not block approval.
    """
    _reserve_ctx(order_ctx, 4, minutes_ago=5)
    approve_order(order_ctx["session"], order_ctx["order"])
    assert order_ctx["order"].estado is OrderEstado.APPROVED
    assert order_ctx["order"].needs_requote is False
