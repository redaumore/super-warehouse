"""Approval orchestration: APPROVE → convert → Sheets → stock → confirm.

Composes the lifecycle approve transition with the registration side effects
that complete an approved order (design data flow):

    approve → reservations ACTIVE→CONVERTED → append Sheets row → deduct stock
            → confirm to the owner (in chat).

``approve_and_register`` is the full flow for a clean approval; it raises
``RequiresRequoteError`` (from the lifecycle) when the order's reservations
have expired — the caller re-quotes first, never approving silently.

``register_approved_order`` is the registration half alone, for approvals that
already ran the lifecycle transition with per-line adjustments
(``dispatch.apply_decision`` + adjustments → then register).

Approval is ATOMIC with registration: when the Sheets write quarantines, the
whole approval is rolled back — the order stays PENDING rather than
half-registered — and the caller replies with an error. The old ``notifier`` /
``owner_phone`` push is gone; the confirmation/error text rides the
``ApprovalResult`` / the caller's reply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Inventory, Order, ReservationEstado, StockReservation
from src.integrations.sheets import SheetsWriter, SheetsWriteStatus
from src.order_lifecycle.state import approve_order

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")


class SheetsRegistrationError(Exception):
    """The approval could not be registered in Google Sheets.

    Raised so the caller rolls the transaction back: the order must stay
    PENDING rather than half-registered (spec: Sheets failure keeps the order
    pending and the owner gets an error reply in chat).
    """


@dataclass(frozen=True)
class ApprovalResult:
    """Outcome of registering an approved order."""

    order: Order
    converted: int
    sheets_status: SheetsWriteStatus
    total: Decimal
    confirmation_text: str


def order_total(order: Order) -> Decimal:
    """Sum of every line's final price × quantity, HALF_UP to the cent."""
    return sum(
        (item.final_price * item.cantidad for item in order.items),
        Decimal(0),
    ).quantize(_CENT, rounding=ROUND_HALF_UP)


def build_items_summary(order: Order) -> str:
    """Compact per-line summary for the Sheets row, e.g. '10 × CLV-001'."""
    return "; ".join(f"{item.cantidad} × {item.sku}" for item in order.items)


def _active_reservations(session: Session, order: Order) -> list[StockReservation]:
    return list(
        session.scalars(
            select(StockReservation).where(
                StockReservation.order_id == order.order_id,
                StockReservation.estado == ReservationEstado.ACTIVE,
            )
        )
    )


def _convert_reservations(
    session: Session, order: Order, reservations: list[StockReservation]
) -> int:
    """Mark every ACTIVE reservation CONVERTED; returns how many."""
    for reservation in reservations:
        reservation.estado = ReservationEstado.CONVERTED
    return len(reservations)


def _deduct_stock(session: Session, reservations: list[StockReservation]) -> None:
    """Subtract each converted reservation's quantity from the canonical Inventory.

    ``Inventory.quantity_on_hand`` is the single on-hand source; the legacy
    ``catalogo.stock_disponible`` counter is deliberately left untouched.
    """
    for reservation in reservations:
        row = session.scalar(select(Inventory).where(Inventory.sku_id == reservation.sku))
        if row is None:
            logger.warning(
                "stock deduction skipped: unknown sku %s (reservation %s)",
                reservation.sku,
                reservation.reservation_id,
            )
            continue
        row.quantity_on_hand -= reservation.cantidad
        row.updated_at = datetime.now(UTC)


def _confirmation_text(order: Order, total: Decimal) -> str:
    return (
        f"Pedido #{order.order_id} aprobado — total {total:.2f} ARS. "
        "Stock descontado. Registrado en Google Sheets."
    )


def register_approved_order(
    session: Session,
    order: Order,
    *,
    sheets: SheetsWriter,
    customer_name: str | None = None,
) -> ApprovalResult:
    """Register an already-APPROVED order: convert, Sheets, deduct, confirm.

    Atomic with registration: a quarantined Sheets write raises
    ``SheetsRegistrationError`` so the caller rolls the approval back — the
    order stays PENDING rather than half-registered.
    """
    reservations = _active_reservations(session, order)
    converted = _convert_reservations(session, order, reservations)
    total = order_total(order)
    sheets_status = sheets.append_order_row(
        order.order_id,
        customer_name=customer_name
        or (order.customer.nombre_comercial if order.customer else None),
        total=str(total),
        items_summary=build_items_summary(order),
    )
    if sheets_status is SheetsWriteStatus.QUARANTINED:
        raise SheetsRegistrationError(
            f"order {order.order_id} could not be registered in Google Sheets"
        )
    _deduct_stock(session, reservations)
    session.flush()
    return ApprovalResult(
        order=order,
        converted=converted,
        sheets_status=sheets_status,
        total=total,
        confirmation_text=_confirmation_text(order, total),
    )


def approve_and_register(
    session: Session,
    order: Order,
    *,
    sheets: SheetsWriter,
    customer_name: str | None = None,
    now: datetime | None = None,
) -> ApprovalResult:
    """Full flow: lifecycle approve (refuses stale orders) then register.

    Raises ``RequiresRequoteError`` when the order has TTL-expired
    reservations — the caller must re-quote before registration. A Sheets
    quarantine propagates as ``SheetsRegistrationError`` for the caller to
    roll back.
    """
    approve_order(session, order, now=now)
    return register_approved_order(
        session,
        order,
        sheets=sheets,
        customer_name=customer_name,
    )
