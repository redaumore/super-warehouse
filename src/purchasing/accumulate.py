"""Purchase order accumulation: one OPEN PO per supplier, items by SKU.

Per the purchase-order-lifecycle spec:

- a later customer order selecting the same supplier merges into the supplier's
  existing OPEN purchase order instead of creating a duplicate (items aggregate
  by SKU);
- selecting several suppliers produces one PO per supplier, each holding only
  that supplier's items.

``accumulate_need`` also persists the owner's selection on the ``SourcingNeed``
row (DB source of truth) and, when the owner re-selects a different supplier
before execution, removes the previously accumulated quantity from the old OPEN
PO so no SKU is double-ordered.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    SourcingNeed,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)


def open_or_create_po(session: Session, supplier_id: int) -> SupplierPurchaseOrder:
    """Return the supplier's OPEN purchase order, creating one when absent."""
    po = session.scalar(
        select(SupplierPurchaseOrder).where(
            SupplierPurchaseOrder.supplier_id == supplier_id,
            SupplierPurchaseOrder.estado == SupplierPurchaseOrderState.OPEN,
        )
    )
    if po is None:
        po = SupplierPurchaseOrder(supplier_id=supplier_id, estado=SupplierPurchaseOrderState.OPEN)
        session.add(po)
        session.flush()
    return po


class SelectionExecutedError(Exception):
    """A need's selection was already executed (its PO is SENT or later)."""


def accumulate_need(
    session: Session,
    need: SourcingNeed,
    supplier_id: int,
) -> SupplierPurchaseOrder:
    """Accumulate one sourcing need into the supplier's OPEN purchase order.

    The selection is persisted on the need. When the need already belongs to a
    different supplier's OPEN PO (owner re-selection before execution), the old
    quantity is detached first so the SKU is never double-ordered. Re-selecting
    after the previous PO was executed (SENT or later) raises
    ``SelectionExecutedError`` — the owner must not re-order an executed line.
    """
    if need.supplier_id is not None and need.supplier_id == supplier_id:
        # Same supplier re-selected: the need is already accumulated there.
        item = session.get(SupplierPurchaseOrderItem, need.po_item_id) if need.po_item_id else None
        if item is not None:
            po = session.get(SupplierPurchaseOrder, item.po_id)
            if po is not None:
                return po
    if need.supplier_id is not None and need.supplier_id != supplier_id:
        _guard_previous_po_not_executed(session, need)
        _detach_from_previous_po(session, need)
    po = open_or_create_po(session, supplier_id)
    item = session.scalar(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po.po_id,
            SupplierPurchaseOrderItem.sku == need.sku,
        )
    )
    if item is None:
        item = SupplierPurchaseOrderItem(
            po_id=po.po_id,
            sku=need.sku,
            quantity=need.missing_quantity,
            received_quantity=0,
        )
        session.add(item)
        session.flush()
    else:
        item.quantity += need.missing_quantity
    need.supplier_id = supplier_id
    need.po_item_id = item.po_item_id
    session.flush()
    return po


def _guard_previous_po_not_executed(session: Session, need: SourcingNeed) -> None:
    """Refuse a re-selection whose previous PO is no longer OPEN."""
    if need.po_item_id is None:
        return
    item = session.get(SupplierPurchaseOrderItem, need.po_item_id)
    if item is None:
        return
    po = session.get(SupplierPurchaseOrder, item.po_id)
    if po is not None and po.estado is not SupplierPurchaseOrderState.OPEN:
        raise SelectionExecutedError(
            f"selection for sku {need.sku} already executed on PO {po.po_id}"
        )


def _detach_from_previous_po(session: Session, need: SourcingNeed) -> None:
    """Remove the need's quantity from its previous (OPEN) PO item, if any."""
    if need.po_item_id is None:
        return
    item = session.get(SupplierPurchaseOrderItem, need.po_item_id)
    if item is None:
        need.po_item_id = None
        return
    po = session.get(SupplierPurchaseOrder, item.po_id)
    if po is None or po.estado is not SupplierPurchaseOrderState.OPEN:
        # Already executed (SENT or later): the need keeps its original link.
        return
    item.quantity -= need.missing_quantity
    if item.quantity <= 0:
        session.delete(item)
    need.po_item_id = None
