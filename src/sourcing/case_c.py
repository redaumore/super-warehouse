"""Case C: no-supplier orders are cancelled and the owner is notified in chat.

Per the order-sourcing spec, an order whose missing items cannot be sourced is
persisted as a Draft and then cancelled through the generalized cancel path —
``cancel_order`` releases any reservation the order holds and sets OrderEstado
CANCELED — while its sourcing axis is set to CANCELLED. The unavailability
message travels as the agent's in-chat reply; the separate Telegram push to
``owner_phone`` was removed. Per design AD9, cancelling never touches purchase
orders or ``SourcingNeed`` rows (Case C holds none).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from src.db.models import Cliente, Order, SourcingState
from src.order_lifecycle.state import cancel_order


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


def cancel_for_no_supplier(session: Session, order: Order, *, actor: str = "owner") -> Order:
    """Cancel the order and mark its sourcing axis CANCELLED.

    ``cancel_order`` is the generalized cancel path: it releases every ACTIVE
    reservation (Draft/Confirmed) and sets OrderEstado CANCELED. The owner is
    told via the agent's in-chat reply — no separate notification push.
    """
    cancel_order(session, order, actor=actor)
    order.sourcing_state = SourcingState.CANCELLED
    session.flush()
    return order
