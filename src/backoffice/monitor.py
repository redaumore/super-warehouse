"""Backoffice live order monitor (task 3.7).

Pure DB operations behind the Gradio monitor tab: every order with its state
(Pending Approval / Approved / In Dispatch / Rejected), its soft-lock status
(ACTIVE reservation count per order) and — when a SheetsWriter is supplied —
whether the order was registered in Google Sheets.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import Order, ReservationEstado, StockReservation
from src.integrations.sheets import SheetsWriter


def list_orders(
    session: Session,
    sheets: SheetsWriter | None = None,
) -> list[dict[str, object]]:
    """Order rows for the monitor: state, soft-lock summary, Sheets sync status."""
    active_counts: dict[int, int] = {
        order_id: int(count)
        for order_id, count in session.execute(
            select(
                StockReservation.order_id,
                func.count(StockReservation.reservation_id),
            )
            .where(StockReservation.estado == ReservationEstado.ACTIVE)
            .group_by(StockReservation.order_id)
        )
    }
    rows = []
    for order in session.scalars(select(Order).order_by(Order.order_id.desc())):
        customer_name = order.customer.nombre_comercial if order.customer else "—"
        rows.append(
            {
                "order_id": order.order_id,
                "customer": customer_name,
                "estado": order.estado.value,
                "needs_requote": order.needs_requote,
                "active_reservations": int(active_counts.get(order.order_id, 0)),
                "sheets_synced": bool(sheets is not None and sheets.sheets_synced(order.order_id)),
            }
        )
    return rows
