"""Approval orchestration (task 3.4): APPROVE → convert → Sheets → stock → confirm.

Composes the lifecycle approve transition with the registration side effects
that complete an approved order (design data flow):

    approve → reservations ACTIVE→CONVERTED → append Sheets row → deduct stock
            → confirm to the owner.

``approve_and_register`` is the full flow for a clean approval; it raises
``RequiresRequoteError`` (from the lifecycle) when the order's reservations
have expired — the caller re-quotes first, never approving silently.

``register_approved_order`` is the registration half alone, for approvals that
already ran the lifecycle transition with per-line adjustments
(``dispatch.apply_decision`` + adjustments → then register). Sheets failures
never block the flow: the append-only writer quarantines internally and the
confirmation message tells the owner when the Sheets registration is pending.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.dispatch import Notifier
from src.db.models import Catalogo, Order, ReservationEstado, StockReservation
from src.integrations.sheets import SheetsWriter, SheetsWriteStatus
from src.order_lifecycle.state import approve_order

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class ApprovalResult:
    """Outcome of registering an approved order."""

    order: Order
    converted: int
    sheets_status: SheetsWriteStatus
    total: Decimal


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
    """Subtract each converted reservation's quantity from the catalog stock."""
    for reservation in reservations:
        product = session.scalar(
            select(Catalogo).where(Catalogo.codigo_interno == reservation.sku)
        )
        if product is None:
            logger.warning(
                "stock deduction skipped: unknown sku %s (reservation %s)",
                reservation.sku,
                reservation.reservation_id,
            )
            continue
        product.stock_disponible -= reservation.cantidad


def _confirmation_text(order: Order, total: Decimal, sheets_status: SheetsWriteStatus) -> str:
    registration = (
        "Registrado en Google Sheets."
        if sheets_status is SheetsWriteStatus.APPENDED
        else "Aviso: el registro en Google Sheets quedó en cuarentena (revisar el backoffice)."
    )
    return (
        f"Pedido #{order.order_id} aprobado — total {total:.2f} ARS. "
        f"Stock descontado. {registration}"
    )


def register_approved_order(
    session: Session,
    order: Order,
    *,
    sheets: SheetsWriter,
    notifier: Notifier,
    owner_phone: str,
    customer_name: str | None = None,
) -> ApprovalResult:
    """Register an already-APPROVED order: convert, Sheets, deduct, confirm.

    Sheets failures quarantine internally (append-only writer contract) and
    never raise; the confirmation still reaches the owner.
    """
    reservations = _active_reservations(session, order)
    converted = _convert_reservations(session, order, reservations)
    total = order_total(order)
    sheets_status = sheets.append_order_row(
        order.order_id,
        customer_name=customer_name,
        total=str(total),
        items_summary=build_items_summary(order),
    )
    _deduct_stock(session, reservations)
    notifier.send_text(owner_phone, _confirmation_text(order, total, sheets_status))
    session.flush()
    return ApprovalResult(order=order, converted=converted, sheets_status=sheets_status, total=total)


def approve_and_register(
    session: Session,
    order: Order,
    *,
    sheets: SheetsWriter,
    notifier: Notifier,
    owner_phone: str,
    customer_name: str | None = None,
    now: datetime | None = None,
) -> ApprovalResult:
    """Full flow: lifecycle approve (refuses stale orders) then register.

    Raises ``RequiresRequoteError`` when the order has TTL-expired
    reservations — the caller must re-quote before registration.
    """
    approve_order(session, order, now=now)
    return register_approved_order(
        session,
        order,
        sheets=sheets,
        notifier=notifier,
        owner_phone=owner_phone,
        customer_name=customer_name,
    )