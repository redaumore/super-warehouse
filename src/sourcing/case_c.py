"""Case C: no-supplier orders are rejected and the owner is notified in chat.

Per the order-sourcing spec, an order whose missing items cannot be sourced
moves through the existing rejection flow — ``reject_order`` releases any
reservation the order holds and sets OrderEstado REJECTED — and its sourcing
axis is set to CANCELLED. The unavailability message travels as the agent's
in-chat reply; the separate Telegram push to ``owner_phone`` was removed.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from src.db.models import Cliente, Order, SourcingState
from src.order_lifecycle.state import reject_order


def persist_case_c_order(
    session: Session,
    customer: Cliente,
    *,
    delivery_date: date | None = None,
) -> Order:
    """Persist the order that will be cancelled for lack of suppliers."""
    order = Order(customer_id=customer.customer_id, delivery_date=delivery_date)
    session.add(order)
    session.flush()
    return order


def cancel_for_no_supplier(session: Session, order: Order) -> Order:
    """Reject the order and mark its sourcing axis CANCELLED.

    ``reject_order`` is the existing rejection flow: it releases every ACTIVE
    reservation immediately and sets OrderEstado REJECTED. The owner is told
    via the agent's in-chat reply — no separate notification push.
    """
    reject_order(session, order)
    order.sourcing_state = SourcingState.CANCELLED
    session.flush()
    return order
