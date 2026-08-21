"""Inventory soft-lock tests (task 2.5).

Integration tests against the real Postgres fixture proving the availability
formula `available = stock_disponible − Σ(active, unexpired reservations)` and
the reservation guard. The TTL sweeper / reject→release state machine are later
phases and are NOT exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from src.agents.inventory import InsufficientStockError, available_stock, reserve_stock
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Proveedor,
    ReservationEstado,
    StockReservation,
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
def stock(db_session):
    """Seed one product with 10 units on hand and a registered customer."""
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
    return db_session


def _reserve(session, cantidad, *, estado=ReservationEstado.ACTIVE, timestamp=None, ttl=30):
    reservation = StockReservation(
        sku="CLV-001",
        customer_id=1,
        cantidad=cantidad,
        ttl_minutes=ttl,
        estado=estado,
    )
    if timestamp is not None:
        reservation.timestamp = timestamp
    session.add(reservation)
    session.flush()
    return reservation


def test_available_equals_stock_without_reservations(stock):
    assert available_stock(stock, "CLV-001") == 10


def test_active_reservation_reduces_availability(stock):
    _reserve(stock, 3)
    assert available_stock(stock, "CLV-001") == 7


def test_multiple_active_reservations_accumulate(stock):
    _reserve(stock, 3)
    _reserve(stock, 2)
    assert available_stock(stock, "CLV-001") == 5


def test_non_active_reservations_do_not_lock_stock(stock):
    _reserve(stock, 3, estado=ReservationEstado.CONVERTED)
    _reserve(stock, 2, estado=ReservationEstado.RELEASED)
    _reserve(stock, 1, estado=ReservationEstado.EXPIRED)
    assert available_stock(stock, "CLV-001") == 10


def test_expired_ttl_reservation_does_not_lock_stock(stock):
    """An ACTIVE reservation past its TTL is excluded at read time."""
    expired = datetime.now(UTC) - timedelta(minutes=31)
    _reserve(stock, 4, timestamp=expired, ttl=30)
    assert available_stock(stock, "CLV-001") == 10


def test_unexpired_reservation_still_locks_stock(stock):
    fresh = datetime.now(UTC) - timedelta(minutes=5)
    _reserve(stock, 4, timestamp=fresh, ttl=30)
    assert available_stock(stock, "CLV-001") == 6


def test_unknown_sku_raises(stock):
    with pytest.raises(KeyError, match="CLV-XXX"):
        available_stock(stock, "CLV-XXX")


def test_reserve_creates_active_reservation_and_locks(stock):
    reservation = reserve_stock(stock, "CLV-001", customer_id=1, cantidad=3)
    assert reservation.estado is ReservationEstado.ACTIVE
    assert reservation.ttl_minutes == get_settings().reservation_ttl_minutes
    assert available_stock(stock, "CLV-001") == 7


def test_reserve_beyond_available_stock_is_refused(stock):
    _reserve(stock, 8)
    with pytest.raises(InsufficientStockError):
        reserve_stock(stock, "CLV-001", customer_id=1, cantidad=5)
    assert available_stock(stock, "CLV-001") == 2  # nothing extra was locked


def test_reserve_rejects_non_positive_quantity(stock):
    with pytest.raises(ValueError, match="positive"):
        reserve_stock(stock, "CLV-001", customer_id=1, cantidad=0)
