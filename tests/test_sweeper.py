"""Scheduler sweeper tests (task 2.10 + 2.11 RED: TTL expiry release).

Integration (Postgres, skipped when down): the RED scenario — a reservation
past its 30-minute TTL is released (EXPIRED) and the order is flagged
``needs_requote``; fresh reservations are untouched; stock becomes available
again.

Unit (no DB): the tick wraps the sweep in a transaction — commit on success,
rollback + log on failure — and the scheduler exposes the interval job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.inventory import available_stock
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
from src.scheduler.sweeper import _tick, build_sweeper, sweep_expired


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


# ------------------------------------------------- unit: scheduler wiring


class _FakeSession:
    def __init__(self, *, fail: bool = False, closed: list | None = None):
        self.fail = fail
        self.closed = closed
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        if self.closed is not None:
            self.closed.append(self)

    def flush(self):
        if self.fail:
            raise RuntimeError("db exploded")

    def scalars(self, _statement):
        return _ScalarResult([])


class _ScalarResult:
    """Duck-typed stand-in for SQLAlchemy's ScalarResult."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


def test_sweep_tick_commits_on_success():
    """El tick del sweeper hace commit al terminar con éxito."""
    session = _FakeSession()
    _tick(lambda: session)
    assert session.committed is True
    assert session.rolled_back is False


def test_sweep_tick_rolls_back_and_keeps_scheduler_alive_on_failure():
    """Ante un fallo, el tick hace rollback y el scheduler sigue vivo."""
    session = _FakeSession(fail=True)
    _tick(lambda: session)  # must not raise — the scheduler keeps running
    assert session.rolled_back is True
    assert session.committed is False


def test_build_sweeper_registers_interval_job():
    """El scheduler registra el job de intervalo del sweeper."""
    scheduler = build_sweeper(lambda: _FakeSession(), interval_minutes=2)
    try:
        job = scheduler.get_job("reservation-ttl-sweep")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 120
    finally:
        if scheduler.state == 1:  # STATE_RUNNING — shutdown only when started
            scheduler.shutdown(wait=False)


# ------------------------------------------------- integration: TTL RED (2.11)


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
    """Seed product (10 units), customer and a PENDING_APPROVAL order."""
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
    return {"session": db_session, "order": order, "sku": "CLV-001"}


def _reservation(ctx, cantidad, *, minutes_ago: int):
    reservation = StockReservation(
        sku=ctx["sku"],
        customer_id=1,
        order_id=ctx["order"].order_id,
        cantidad=cantidad,
        ttl_minutes=30,
        estado=ReservationEstado.ACTIVE,
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    ctx["session"].add(reservation)
    ctx["session"].flush()
    return reservation


def test_sweep_expires_past_ttl_and_flags_order(order_ctx):
    """El sweeper expira reservas vencidas por TTL y marca el pedido.

    RED (2.11): TTL expiry releases the reservation and stock is available.

    Availability excludes the expired reservation at READ time already (the
    design's read-time TTL correctness); the sweeper makes the release durable
    — EXPIRED state + the order flagged for re-quote.
    """
    _reservation(order_ctx, 4, minutes_ago=31)
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10  # excluded at read

    count = sweep_expired(order_ctx["session"])

    assert count == 1
    reservation = order_ctx["session"].scalar(
        select(StockReservation).where(StockReservation.order_id == order_ctx["order"].order_id)
    )
    assert reservation.estado is ReservationEstado.EXPIRED
    assert order_ctx["order"].needs_requote is True
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10  # stays released


def test_sweep_leaves_fresh_reservations_active(order_ctx):
    """El sweeper deja activas las reservas vigentes."""
    _reservation(order_ctx, 4, minutes_ago=5)
    assert sweep_expired(order_ctx["session"]) == 0
    reservation = order_ctx["session"].scalar(
        select(StockReservation).where(StockReservation.order_id == order_ctx["order"].order_id)
    )
    assert reservation.estado is ReservationEstado.ACTIVE
    assert order_ctx["order"].needs_requote is False


def test_sweep_expires_only_past_ttl_among_mixed(order_ctx):
    """Entre reservas mixtas, el sweeper expira solo las vencidas."""
    _reservation(order_ctx, 4, minutes_ago=31)
    _reservation(order_ctx, 2, minutes_ago=5)
    assert sweep_expired(order_ctx["session"]) == 1
    reservations = order_ctx["session"].scalars(
        select(StockReservation).where(StockReservation.order_id == order_ctx["order"].order_id)
    ).all()
    states = sorted(r.estado.value for r in reservations)
    assert states == ["ACTIVE", "EXPIRED"]
