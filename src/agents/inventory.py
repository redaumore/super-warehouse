"""Inventory agent: soft-lock reservations and available stock.

Availability follows the design formula:

    available = stock_disponible − Σ(cantidad of ACTIVE, unexpired reservations)

Only the soft-lock calculation lives here (task 2.5). Expiry is honored at read
time by filtering reservations whose `timestamp + ttl_minutes` is still in the
future; the TTL sweeper, the reject→release transitions and the order state
machine are later phases.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Catalogo, ReservationEstado, StockReservation


class InsufficientStockError(Exception):
    """Raised when a reservation would exceed the available stock."""


def available_stock(session: Session, sku: str, *, now: datetime | None = None) -> int:
    """Available stock = `stock_disponible` − Σ(active, unexpired reservations).

    ``now`` is injectable for tests; defaults to the current UTC time.
    """
    reference = now or datetime.now(UTC)
    # make_interval(years, months, weeks, days, hours, mins, secs) — positional
    # so the per-row TTL column lands in the minutes slot.
    expires_at = StockReservation.timestamp + func.make_interval(
        0, 0, 0, 0, 0, StockReservation.ttl_minutes
    )
    locked = (
        select(func.coalesce(func.sum(StockReservation.cantidad), 0))
        .where(
            StockReservation.sku == sku,
            StockReservation.estado == ReservationEstado.ACTIVE,
            expires_at > reference,
        )
        .scalar_subquery()
    )
    available = session.execute(
        select(Catalogo.stock_disponible - locked).where(Catalogo.codigo_interno == sku)
    ).scalar()
    if available is None:
        raise KeyError(f"unknown sku: {sku}")
    return int(available)


def reserve_stock(
    session: Session,
    sku: str,
    customer_id: int,
    cantidad: int,
    *,
    order_id: int | None = None,
    ttl_minutes: int | None = None,
) -> StockReservation:
    """Create an ACTIVE soft-lock reservation; refuse when stock is insufficient."""
    if cantidad <= 0:
        raise ValueError("cantidad must be positive")
    if available_stock(session, sku) < cantidad:
        raise InsufficientStockError(f"insufficient available stock for sku {sku}")
    reservation = StockReservation(
        sku=sku,
        customer_id=customer_id,
        order_id=order_id,
        cantidad=cantidad,
        ttl_minutes=ttl_minutes or get_settings().reservation_ttl_minutes,
        estado=ReservationEstado.ACTIVE,
    )
    session.add(reservation)
    session.flush()
    return reservation
