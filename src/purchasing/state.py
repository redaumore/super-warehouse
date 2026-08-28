"""Supplier purchase order state machine (mirrors ``src/order_lifecycle/state.py``).

The ``SupplierPurchaseOrderState`` enum is declared with the data model
(``src.db.models``); this module owns the TRANSITIONS: which moves are legal
and what side effects each move has.

Per the purchase-order-lifecycle spec:

- ``OPEN`` → ``SENT`` (owner sends it to the supplier) → ``PARTIALLY_RECEIVED``
  (partial receipt) → ``FULLY_RECEIVED`` (terminal, all lines received);
- ``OPEN`` or ``SENT`` → ``CANCELLED`` (terminal);
- any other move (including any transition from ``FULLY_RECEIVED`` or
  ``CANCELLED``) raises ``InvalidTransitionError``.

Receiving also bumps the canonical ``Inventory.quantity_on_hand`` by the
received delta (the supplier goods arrive into local stock).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    Inventory,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderState,
)


class InvalidTransitionError(Exception):
    """Raised when a purchase order cannot move between two states."""


def send_po(
    session: Session,
    po: SupplierPurchaseOrder,
    *,
    now: datetime | None = None,
) -> SupplierPurchaseOrder:
    """Move an OPEN purchase order to SENT (owner sends it to the supplier)."""
    if po.estado is not SupplierPurchaseOrderState.OPEN:
        raise InvalidTransitionError(f"cannot send purchase order in state {po.estado.value}")
    po.estado = SupplierPurchaseOrderState.SENT
    po.sent_at = now or datetime.now(UTC)
    session.flush()
    return po


def cancel_po(
    session: Session,
    po: SupplierPurchaseOrder,
    *,
    now: datetime | None = None,
) -> SupplierPurchaseOrder:
    """Cancel an OPEN or SENT purchase order (terminal state)."""
    if po.estado not in (
        SupplierPurchaseOrderState.OPEN,
        SupplierPurchaseOrderState.SENT,
    ):
        raise InvalidTransitionError(f"cannot cancel purchase order in state {po.estado.value}")
    po.estado = SupplierPurchaseOrderState.CANCELLED
    po.cancelled_at = now or datetime.now(UTC)
    session.flush()
    return po


def receive_po(
    session: Session,
    po: SupplierPurchaseOrder,
    received: Mapping[str, int],
    *,
    now: datetime | None = None,
) -> SupplierPurchaseOrder:
    """Record a partial or full receipt.

    Legal from SENT and PARTIALLY_RECEIVED. Each SKU's ``received_quantity``
    grows by the received delta and the canonical ``Inventory`` gains the same
    units; once every line is fully received the PO moves to
    FULLY_RECEIVED (terminal), otherwise PARTIALLY_RECEIVED.
    """
    if po.estado not in (
        SupplierPurchaseOrderState.SENT,
        SupplierPurchaseOrderState.PARTIALLY_RECEIVED,
    ):
        raise InvalidTransitionError(f"cannot receive purchase order in state {po.estado.value}")
    reference = now or datetime.now(UTC)
    by_sku = {item.sku: item for item in po.items}
    for sku, quantity in received.items():
        if quantity <= 0:
            raise ValueError("received quantity must be positive")
        item = by_sku.get(sku)
        if item is None:
            raise KeyError(f"sku {sku} not in purchase order")
        if item.received_quantity + quantity > item.quantity:
            raise ValueError(f"received {quantity} exceeds the remaining quantity for {sku}")
        item.received_quantity += quantity
        inventory_row = session.scalar(select(Inventory).where(Inventory.sku_id == sku))
        if inventory_row is None:
            inventory_row = Inventory(sku_id=sku, quantity_on_hand=0)
            session.add(inventory_row)
        inventory_row.quantity_on_hand += quantity
        inventory_row.updated_at = reference
    if all(item.received_quantity >= item.quantity for item in po.items):
        po.estado = SupplierPurchaseOrderState.FULLY_RECEIVED
    else:
        po.estado = SupplierPurchaseOrderState.PARTIALLY_RECEIVED
    po.received_at = reference
    session.flush()
    return po
