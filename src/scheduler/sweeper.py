"""APScheduler sweeper: durable release of TTL-expired reservations (task 2.10).

The order-lifecycle spec requires unapproved orders to be auto-released after
the 30-minute window. TTL correctness is ALREADY enforced at read time — the
inventory agent excludes ACTIVE reservations past their TTL from availability —
so this sweeper is best-effort cleanup that makes the expiry durable: it marks
past-TTL ACTIVE reservations as EXPIRED and flags their orders with
``needs_requote`` so any later approval attempt must re-quote first (the
lifecycle refuses stale approvals anyway).

``build_sweeper`` wraps the job in APScheduler's ``BackgroundScheduler``;
``sweep_expired`` is the pure-ish DB unit so it can be tested directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import Order, ReservationEstado, StockReservation

logger = logging.getLogger(__name__)


def _expires_at_expr() -> Any:
    """SQL expression: the moment a reservation expires (timestamp + TTL)."""
    return StockReservation.timestamp + func.make_interval(
        0, 0, 0, 0, 0, StockReservation.ttl_minutes
    )


def sweep_expired(session: Session, *, now: datetime | None = None) -> int:
    """Expire every past-TTL ACTIVE reservation and flag its order.

    Returns how many reservations were expired. The caller owns the
    transaction (commit / rollback); this function only flushes so its effects
    are visible within the session.
    """
    reference = now or datetime.now(UTC)
    rows = session.scalars(
        select(StockReservation).where(
            StockReservation.estado == ReservationEstado.ACTIVE,
            _expires_at_expr() <= reference,
        )
    ).all()
    order_ids: set[int] = set()
    for reservation in rows:
        reservation.estado = ReservationEstado.EXPIRED
        if reservation.order_id is not None:
            order_ids.add(reservation.order_id)
    if order_ids:
        orders = session.scalars(select(Order).where(Order.order_id.in_(order_ids))).all()
        for order in orders:
            order.needs_requote = True
    session.flush()
    return len(rows)


def _tick(session_factory: Callable[[], Session]) -> None:
    """One scheduled sweep, wrapped in its own transaction."""
    session = session_factory()
    try:
        count = sweep_expired(session)
        session.commit()
        if count:
            logger.info("sweeper expired %d reservation(s)", count)
    except Exception:
        session.rollback()
        logger.exception("sweeper tick failed")
    finally:
        session.close()


def build_sweeper(
    session_factory: Callable[[], Session],
    *,
    interval_minutes: int = 1,
) -> BackgroundScheduler:
    """Build a background scheduler running the TTL sweep every ``interval_minutes``."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _tick,
        "interval",
        minutes=interval_minutes,
        args=[session_factory],
        id="reservation-ttl-sweep",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler