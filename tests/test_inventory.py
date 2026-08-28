"""Inventory soft-lock tests (task 2.5, updated for the sourcing axis).

Integration tests against the real Postgres fixture proving the availability
formula `available = Inventory.quantity_on_hand − Σ(active, unexpired
reservations)` and the reservation guard. An SKU with no inventory row is
treated as unavailable (zero on hand), never a KeyError. The TTL sweeper /
reject→release state machine are later phases and are NOT exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.inventory import (
    InsufficientStockError,
    available_stock,
    reserve_stock,
    seed_inventory,
)
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
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
                "TRUNCATE supplier_purchase_order_items, supplier_purchase_orders, "
                "sourcing_needs, inventory, order_items, orders, stock_reservations, "
                "catalogo, proveedores, clientes, lista_precios RESTART IDENTITY CASCADE"
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
    seed_inventory(db_session)
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
    """Sin reservas, el stock disponible es todo el stock en mano."""
    assert available_stock(stock, "CLV-001") == 10


def test_active_reservation_reduces_availability(stock):
    """Una reserva activa reduce la disponibilidad."""
    _reserve(stock, 3)
    assert available_stock(stock, "CLV-001") == 7


def test_multiple_active_reservations_accumulate(stock):
    """Varias reservas activas se acumulan al descontar disponibilidad."""
    _reserve(stock, 3)
    _reserve(stock, 2)
    assert available_stock(stock, "CLV-001") == 5


def test_non_active_reservations_do_not_lock_stock(stock):
    """Las reservas no activas (convertidas, liberadas, expiradas) no bloquean stock."""
    _reserve(stock, 3, estado=ReservationEstado.CONVERTED)
    _reserve(stock, 2, estado=ReservationEstado.RELEASED)
    _reserve(stock, 1, estado=ReservationEstado.EXPIRED)
    assert available_stock(stock, "CLV-001") == 10


def test_expired_ttl_reservation_does_not_lock_stock(stock):
    """Una reserva ACTIVE vencida por TTL se excluye al leer la disponibilidad.

    An ACTIVE reservation past its TTL is excluded at read time.
    """
    expired = datetime.now(UTC) - timedelta(minutes=31)
    _reserve(stock, 4, timestamp=expired, ttl=30)
    assert available_stock(stock, "CLV-001") == 10


def test_unexpired_reservation_still_locks_stock(stock):
    """Una reserva vigente todavía bloquea stock."""
    fresh = datetime.now(UTC) - timedelta(minutes=5)
    _reserve(stock, 4, timestamp=fresh, ttl=30)
    assert available_stock(stock, "CLV-001") == 6


def test_unknown_sku_returns_zero(stock):
    """Consultar un SKU desconocido devuelve 0 (nunca KeyError)."""
    assert available_stock(stock, "CLV-XXX") == 0


def test_seed_inventory_backfills_from_catalogo(stock):
    """El seed copia stock_disponible del catálogo a Inventory.quantity_on_hand."""
    row = stock.scalar(select(Inventory).where(Inventory.sku_id == "CLV-001"))
    assert row is not None
    assert row.quantity_on_hand == 10


def test_seed_inventory_is_idempotent(stock):
    """Volver a sembrar no duplica filas ni pisa valores existentes."""
    first = seed_inventory(stock)  # the fixture already seeded → no insert
    assert first == 0
    row = stock.scalar(select(Inventory).where(Inventory.sku_id == "CLV-001"))
    row.quantity_on_hand = 7
    stock.flush()
    second = seed_inventory(stock)
    assert second == 0
    assert (
        stock.scalar(select(Inventory).where(Inventory.sku_id == "CLV-001")).quantity_on_hand == 7
    )


def test_missing_inventory_row_means_zero_on_hand(stock):
    """Un SKU sin fila en Inventory se trata como no disponible."""
    assert available_stock(stock, "OTRO-999") == 0


def test_reserve_creates_active_reservation_and_locks(stock):
    """Reservar crea una reserva activa con el TTL configurado y bloquea stock."""
    reservation = reserve_stock(stock, "CLV-001", customer_id=1, cantidad=3)
    assert reservation.estado is ReservationEstado.ACTIVE
    assert reservation.ttl_minutes == get_settings().reservation_ttl_minutes
    assert available_stock(stock, "CLV-001") == 7


def test_reserve_beyond_available_stock_is_refused(stock):
    """Reservar más de lo disponible se rechaza sin bloquear de más."""
    _reserve(stock, 8)
    with pytest.raises(InsufficientStockError):
        reserve_stock(stock, "CLV-001", customer_id=1, cantidad=5)
    assert available_stock(stock, "CLV-001") == 2  # nothing extra was locked


def test_reserve_rejects_non_positive_quantity(stock):
    """Reservar una cantidad no positiva se rechaza."""
    with pytest.raises(ValueError, match="positive"):
        reserve_stock(stock, "CLV-001", customer_id=1, cantidad=0)
