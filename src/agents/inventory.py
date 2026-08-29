"""Inventory agent: soft-lock reservations and available stock.

Availability follows the design formula:

    available = Inventory.quantity_on_hand − Σ(cantidad of ACTIVE, unexpired reservations)

``Inventory`` is the single on-hand source (backfilled from
``catalogo.stock_disponible``); an SKU with no inventory row is treated as
unavailable (zero on hand). Expiry is honored at read time by filtering
reservations whose `timestamp + ttl_minutes` is still in the future; the TTL
sweeper, the reject→release transitions and the order state machine are later
phases.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Catalogo, Inventory, ReservationEstado, StockReservation


class InsufficientStockError(Exception):
    """Raised when a reservation would exceed the available stock."""


def available_stock(session: Session, sku: str, *, now: datetime | None = None) -> int:
    """Available stock = `Inventory.quantity_on_hand` − Σ(active, unexpired reservations).

    An SKU with no inventory row (unknown to the catalog/inventory) returns 0 —
    it is treated as unavailable, never a KeyError. ``now`` is injectable for
    tests; defaults to the current UTC time.
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
    on_hand = session.execute(
        select(Inventory.quantity_on_hand - locked).where(Inventory.sku_id == sku)
    ).scalar()
    if on_hand is None:
        return 0
    return int(on_hand)


def seed_inventory(session: Session) -> int:
    """Backfill `Inventory` from each catalog product's `stock_disponible`.

    Idempotent: rows already present (INSERT … ON CONFLICT DO NOTHING) keep
    their current on-hand value. Returns how many rows were inserted.
    """
    result = session.execute(
        insert(Inventory)
        .from_select(
            [Inventory.sku_id, Inventory.quantity_on_hand, Inventory.updated_at],
            select(Catalogo.codigo_interno, Catalogo.stock_disponible, func.now()),
        )
        .on_conflict_do_nothing(index_elements=[Inventory.sku_id])
        .returning(Inventory.sku_id)
    )
    inserted = len(result.all())
    session.flush()
    return inserted


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
