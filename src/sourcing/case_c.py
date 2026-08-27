"""Case C: no-supplier orders are rejected and the customer is notified.

Per the order-sourcing spec, an order whose missing items cannot be sourced
moves through the existing rejection flow — ``reject_order`` releases any
reservation the order holds and sets OrderEstado REJECTED — and its sourcing
axis is set to CANCELLED. The owner is alerted through the injected Notifier
while the customer-facing unavailability message is the agent's reply.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from src.agents.dispatch import Notifier
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


def cancel_for_no_supplier(
    session: Session,
    order: Order,
    *,
    notifier: Notifier,
    owner_phone: str,
    customer_name: str | None = None,
) -> Order:
    """Reject the order and mark its sourcing axis CANCELLED.

    ``reject_order`` is the existing rejection flow: it releases every ACTIVE
    reservation immediately and sets OrderEstado REJECTED. The owner is
    notified so the cancellation is never silent.
    """
    reject_order(session, order)
    order.sourcing_state = SourcingState.CANCELLED
    session.flush()
    who = f" de {customer_name}" if customer_name else ""
    notifier.send_text(
        owner_phone,
        f"Pedido #{order.order_id}{who} cancelado: artículos sin proveedor.",
    )
    return order