"""Order state machine and reservation release rules (task 2.9).

The four-state enum itself is declared with the data model
(``src.db.models.OrderEstado``); this module owns the TRANSITIONS: which moves
are legal, what side effects each move has, and the ``needs_requote`` semantics
for TTL-expired reservations.

Per the order-lifecycle spec:

- ``PENDING_APPROVAL`` → ``APPROVED`` (owner approves) → ``IN_DISPATCH``;
- ``PENDING_APPROVAL`` → ``REJECTED`` and every reservation for the order is
  released immediately, making the stock available to other customers again;
- an order whose reservations have passed their TTL must NOT be approved
  silently: ``approve_order`` refuses and raises ``RequiresRequoteError`` after
  flagging ``needs_requote`` — the caller re-quotes (or re-confirms
  availability) before registration.

TTL correctness is enforced at read time by the inventory agent; the sweeper
(Phase 2, task 2.10) makes the expiry durable by marking reservations EXPIRED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.db.models import Order, OrderEstado, ReservationEstado, StockReservation


class InvalidTransitionError(Exception):
    """Raised when an order cannot move between two states (e.g. APPROVED → REJECTED)."""


class RequiresRequoteError(Exception):
    """The order has TTL-expired reservations; it must be re-quoted before approval."""


def _reservation_expired_expr() -> Any:
    """SQL expression: reservation timestamp + ttl_minutes (the expiry moment)."""
    return StockReservation.timestamp + func.make_interval(
        0, 0, 0, 0, 0, StockReservation.ttl_minutes
    )


def requires_requote(session: Session, order: Order, *, now: datetime | None = None) -> bool:
    """True when the order is flagged OR has an ACTIVE reservation past its TTL."""
    if order.needs_requote:
        return True
    reference = now or datetime.now(UTC)
    stale = session.scalar(
        select(StockReservation.reservation_id)
        .where(
            StockReservation.order_id == order.order_id,
            StockReservation.estado == ReservationEstado.ACTIVE,
            _reservation_expired_expr() <= reference,
        )
        .limit(1)
    )
    return stale is not None


def approve_order(session: Session, order: Order, *, now: datetime | None = None) -> Order:
    """Move a PENDING_APPROVAL order to APPROVED.

    Refuses to approve an order with expired reservations: it raises
    ``RequiresRequoteError`` (flagging ``needs_requote``) so the caller
    re-quotes before registration — the spec's "expired order cannot be
    approved silently". Converting reservations to CONVERTED happens at
    registration (Phase 3, task 3.4), not here.
    """
    if order.estado is not OrderEstado.PENDING_APPROVAL:
        raise InvalidTransitionError(f"cannot approve order in state {order.estado.value}")
    if requires_requote(session, order, now=now):
        order.needs_requote = True
        session.flush()
        raise RequiresRequoteError("order has expired reservations; re-quote before approval")
    order.estado = OrderEstado.APPROVED
    order.approved_at = now or datetime.now(UTC)
    order.needs_requote = False
    session.flush()
    return order


def reject_order(session: Session, order: Order, *, now: datetime | None = None) -> Order:
    """Reject a PENDING_APPROVAL order and release all its reservations NOW.

    Spec: on rejection every reservation for the order is released immediately
    and the reserved stock becomes available to other customers.
    """
    if order.estado is not OrderEstado.PENDING_APPROVAL:
        raise InvalidTransitionError(f"cannot reject order in state {order.estado.value}")
    session.execute(
        update(StockReservation)
        .where(
            StockReservation.order_id == order.order_id,
            StockReservation.estado == ReservationEstado.ACTIVE,
        )
        .values(estado=ReservationEstado.RELEASED)
    )
    order.estado = OrderEstado.REJECTED
    order.rejected_at = now or datetime.now(UTC)
    order.needs_requote = False
    session.flush()
    return order


def mark_dispatched(session: Session, order: Order, *, now: datetime | None = None) -> Order:
    """Move an APPROVED order to IN_DISPATCH (happy-path terminal transition)."""
    if order.estado is not OrderEstado.APPROVED:
        raise InvalidTransitionError(f"cannot dispatch order in state {order.estado.value}")
    order.estado = OrderEstado.IN_DISPATCH
    session.flush()
    return order


def expire_reservations(session: Session, order: Order, *, now: datetime | None = None) -> int:
    """Mark the order's past-TTL ACTIVE reservations EXPIRED and flag a re-quote.

    This is the durable side of the TTL rule, driven by the sweeper (task 2.10):
    the reservation is EXPIRED and the order can no longer be approved without a
    re-quote. Returns how many reservations were expired.
    """
    reference = now or datetime.now(UTC)
    rows = session.scalars(
        select(StockReservation).where(
            StockReservation.order_id == order.order_id,
            StockReservation.estado == ReservationEstado.ACTIVE,
            _reservation_expired_expr() <= reference,
        )
    ).all()
    for reservation in rows:
        reservation.estado = ReservationEstado.EXPIRED
    if rows:
        order.needs_requote = True
        session.flush()
    return len(rows)
