"""Order state machine transitions (six states).

The enum itself is declared with the data model (``src.db.models.OrderEstado``);
this module owns the TRANSITIONS: which moves are legal, what side effects each
move has, and the ``needs_requote`` semantics for TTL-expired reservations.

Per the order-lifecycle spec the legal transitions are:

| From | To | Trigger |
|------|----|---------|
| Draft | Draft | add / remove product |
| Draft | Confirmed | confirm |
| Draft | Canceled | cancel order |
| Confirmed | Draft | modify |
| Confirmed | Picking | start picking |
| Confirmed | Canceled | cancel order |
| Picking | Ready for delivery | complete picking |
| Picking | Canceled | cancel order |
| Ready for delivery | Closed | deliver |
| Ready for delivery | Canceled | cancel order |

Side effects:

- ``confirm_order`` refuses an order whose reservations passed their TTL
  (``RequiresRequoteError`` after flagging ``needs_requote``) — the caller
  re-quotes before registration; the reservation→conversion and stock
  deduction happen in the confirm ceremony (``src/orchestrator/approval.py``),
  not here.
- ``cancel_order`` releases ACTIVE reservations from Draft/Confirmed and
  restores the deducted stock (with a ``StockAdjustment`` row, reason
  ``order_cancelled`` and the actor) from Picking/Ready for delivery.
- ``modify_order`` (Confirmed → Draft) restores the deducted stock and
  releases the CONVERTED reservations so a re-confirm starts from a fresh
  TTL (design AD6).
- ``add_draft_item`` / ``remove_draft_item`` mutate ``OrderItem`` rows on a
  Draft; an empty Draft stays DRAFT (it is a state, never deleted).

TTL correctness is enforced at read time by the inventory agent; the sweeper
makes the expiry durable by marking reservations EXPIRED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import (
    Inventory,
    Order,
    OrderEstado,
    OrderItem,
    ReservationEstado,
    StockAdjustment,
    StockReservation,
)


class InvalidTransitionError(Exception):
    """Raised when an order cannot move between two states (e.g. Confirmed → Closed)."""


class RequiresRequoteError(Exception):
    """The order has TTL-expired reservations; it must be re-quoted before confirmation."""


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


# ---------------------------------------------------------------- transitions


def confirm_order(session: Session, order: Order, *, now: datetime | None = None) -> Order:
    """Move a Draft order to Confirmed.

    Refuses to confirm an order with expired reservations: it raises
    ``RequiresRequoteError`` (flagging ``needs_requote``) so the caller
    re-quotes first — the spec's "stale quote refused". Converting
    reservations to CONVERTED and deducting stock happen in the confirm
    ceremony, not here.
    """
    if order.estado is not OrderEstado.DRAFT:
        raise InvalidTransitionError(f"cannot confirm order in state {order.estado.value}")
    if requires_requote(session, order, now=now):
        order.needs_requote = True
        session.flush()
        raise RequiresRequoteError("order has expired reservations; re-quote before confirming")
    order.estado = OrderEstado.CONFIRMED
    order.approved_at = now or datetime.now(UTC)
    order.needs_requote = False
    session.flush()
    return order


def start_picking(session: Session, order: Order) -> Order:
    """Move a Confirmed order to Picking (fulfillment begins)."""
    if order.estado is not OrderEstado.CONFIRMED:
        raise InvalidTransitionError(f"cannot start picking in state {order.estado.value}")
    order.estado = OrderEstado.PICKING
    session.flush()
    return order


def complete_picking(session: Session, order: Order) -> Order:
    """Move a Picking order to Ready for delivery."""
    if order.estado is not OrderEstado.PICKING:
        raise InvalidTransitionError(f"cannot complete picking in state {order.estado.value}")
    order.estado = OrderEstado.READY_FOR_DELIVERY
    session.flush()
    return order


def deliver_order(session: Session, order: Order, *, now: datetime | None = None) -> Order:
    """Move a Ready-for-delivery order to Closed and store the delivery date.

    The ``delivery_date`` column is informational (spec: "The system MUST store
    a delivery_date"); when the order has none yet, today's date is stored.
    """
    if order.estado is not OrderEstado.READY_FOR_DELIVERY:
        raise InvalidTransitionError(f"cannot deliver order in state {order.estado.value}")
    order.estado = OrderEstado.CLOSED
    if order.delivery_date is None:
        order.delivery_date = (now or datetime.now(UTC)).date()
    session.flush()
    return order


def _active_reservations(session: Session, order: Order) -> list[StockReservation]:
    return list(
        session.scalars(
            select(StockReservation).where(
                StockReservation.order_id == order.order_id,
                StockReservation.estado == ReservationEstado.ACTIVE,
            )
        ).all()
    )


def _converted_reservations(session: Session, order: Order) -> list[StockReservation]:
    return list(
        session.scalars(
            select(StockReservation).where(
                StockReservation.order_id == order.order_id,
                StockReservation.estado == ReservationEstado.CONVERTED,
            )
        ).all()
    )


def _release_active_reservations(session: Session, order: Order) -> int:
    """Release every ACTIVE reservation of the order; returns how many."""
    rows = _active_reservations(session, order)
    for reservation in rows:
        reservation.estado = ReservationEstado.RELEASED
    session.flush()
    return len(rows)


def _restore_converted_stock(
    session: Session, order: Order, *, actor: str | None = None, reason: str = "order_cancelled"
) -> int:
    """Add every converted reservation's quantity back to Inventory.

    When ``actor`` is given (late cancel), each restoration writes a
    ``StockAdjustment`` row (reason + actor) so the barcode-stock-ops audit
    trail is preserved. The converted reservation is released so it can never
    restore twice.
    """
    restored = 0
    for reservation in _converted_reservations(session, order):
        row = session.scalar(select(Inventory).where(Inventory.sku_id == reservation.sku))
        if row is not None:
            row.quantity_on_hand += reservation.cantidad
            row.updated_at = datetime.now(UTC)
            if actor is not None:
                session.add(
                    StockAdjustment(
                        sku=reservation.sku,
                        delta=reservation.cantidad,
                        reason=reason,
                        actor=actor,
                    )
                )
        reservation.estado = ReservationEstado.RELEASED
        restored += 1
    session.flush()
    return restored


def cancel_order(
    session: Session, order: Order, *, actor: str, now: datetime | None = None
) -> Order:
    """Cancel a Draft/Confirmed/Picking/Ready-for-delivery order.

    Draft and Confirmed release ACTIVE reservations immediately (the reserved
    stock becomes available again); Picking and Ready for delivery restore the
    already-deducted stock and record a ``StockAdjustment`` row per SKU with
    reason ``order_cancelled`` and the acting entity. Every path ends in
    CANCELED — the order row is never deleted (audit trail).
    """
    if order.estado not in (
        OrderEstado.DRAFT,
        OrderEstado.CONFIRMED,
        OrderEstado.PICKING,
        OrderEstado.READY_FOR_DELIVERY,
    ):
        raise InvalidTransitionError(f"cannot cancel order in state {order.estado.value}")
    if order.estado in (OrderEstado.DRAFT, OrderEstado.CONFIRMED):
        _release_active_reservations(session, order)
    else:
        _restore_converted_stock(session, order, actor=actor, reason="order_cancelled")
    order.estado = OrderEstado.CANCELED
    order.rejected_at = now or datetime.now(UTC)
    order.needs_requote = False
    session.flush()
    return order


def modify_order(session: Session, order: Order, *, now: datetime | None = None) -> Order:
    """Move a Confirmed order back to Draft, restoring stock and releasing locks.

    The deducted stock is put back (no audit row — this is not a cancel) and
    the CONVERTED reservations are released so the re-confirm ceremony starts
    from a fresh TTL reservation (design AD6). Sheets is append-only: the
    previous row stays and the re-confirm appends a fresh one.
    """
    if order.estado is not OrderEstado.CONFIRMED:
        raise InvalidTransitionError(f"cannot modify order in state {order.estado.value}")
    _restore_converted_stock(session, order)
    _release_active_reservations(session, order)
    order.estado = OrderEstado.DRAFT
    order.needs_requote = False
    session.flush()
    return order


# ------------------------------------------------------------- draft line edits


def add_draft_item(session: Session, order: Order, sku: str, cantidad: int) -> OrderItem:
    """Upsert one line on a Draft order; the Draft stays DRAFT.

    An existing SKU accumulates quantity; a new SKU appends a row. Only Draft
    orders accept product edits (spec: Draft → Draft on add / remove).
    """
    if order.estado is not OrderEstado.DRAFT:
        raise InvalidTransitionError(f"cannot add items to order in state {order.estado.value}")
    if cantidad <= 0:
        raise ValueError("cantidad must be positive")
    item = session.scalar(
        select(OrderItem).where(OrderItem.order_id == order.order_id, OrderItem.sku == sku)
    )
    if item is None:
        item = OrderItem(
            order_id=order.order_id,
            sku=sku,
            cantidad=cantidad,
            base_price=Decimal(0),
            final_price=Decimal(0),
            adjustment=Decimal(0),
        )
        session.add(item)
    else:
        item.cantidad += cantidad
    session.flush()
    return item


def remove_draft_item(session: Session, order: Order, sku: str) -> None:
    """Delete one line of a Draft order; an empty Draft stays DRAFT.

    The order row itself is never deleted — Draft is a state, and an empty
    draft remains open for the next add.
    """
    if order.estado is not OrderEstado.DRAFT:
        raise InvalidTransitionError(
            f"cannot remove items from order in state {order.estado.value}"
        )
    item = session.scalar(
        select(OrderItem).where(OrderItem.order_id == order.order_id, OrderItem.sku == sku)
    )
    if item is not None:
        session.delete(item)
        session.flush()


def expire_reservations(session: Session, order: Order, *, now: datetime | None = None) -> int:
    """Mark the order's past-TTL ACTIVE reservations EXPIRED and flag a re-quote.

    This is the durable side of the TTL rule, driven by the sweeper: the
    reservation is EXPIRED and the order can no longer be confirmed without a
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
