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

Because POs are SHARED across customer orders (one OPEN PO per supplier),
cancelling a customer order never cancels a whole PO:
``release_order_needs`` detaches only that order's quantities from the OPEN
POs it touched and cancels a PO shell only when no items are left in it.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import (
    SourcingNeed,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)
from src.purchasing.state import cancel_po
from src.sourcing.persistence import sourcing_needs_for_order
from src.supplier.guards import ensure_active_supplier


def open_or_create_po(session: Session, supplier_id: int) -> SupplierPurchaseOrder:
    """Return the supplier's OPEN purchase order, creating one when absent.

    Refuses INACTIVO suppliers (ACTIVO guard) before any write.
    """
    ensure_active_supplier(session, supplier_id)
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
    INACTIVO suppliers are refused (ACTIVO guard) before any write.
    """
    ensure_active_supplier(session, supplier_id)
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
    # Persist the link clear BEFORE the item row is deleted: without an ORM
    # relationship() the unit of work does not order the need's UPDATE before
    # the item's DELETE, and the FK would fire on the delete.
    session.flush()


def release_order_needs(session: Session, order_id: int) -> list[SupplierPurchaseOrder]:
    """Detach every SourcingNeed of an order from its OPEN PO and cancel empty POs.

    Only OPEN POs are touched (executed POs keep their link). A PO whose items
    are all removed is cancelled via ``cancel_po``; a PO still holding other
    orders' items stays OPEN. Returns the POs that were cancelled.
    """
    needs = sourcing_needs_for_order(session, order_id)
    if not needs:
        return []
    # Collect the touched PO ids BEFORE detaching (the items may be deleted).
    po_ids: set[int] = set()
    for need in needs:
        if need.po_item_id is not None:
            item = session.get(SupplierPurchaseOrderItem, need.po_item_id)
            if item is not None:
                po_ids.add(item.po_id)
        _detach_from_previous_po(session, need)
    session.flush()
    cancelled: list[SupplierPurchaseOrder] = []
    for po_id in sorted(po_ids):
        po = session.get(SupplierPurchaseOrder, po_id)
        if po is None or po.estado is not SupplierPurchaseOrderState.OPEN:
            continue
        remaining = session.scalar(
            select(func.count())
            .select_from(SupplierPurchaseOrderItem)
            .where(SupplierPurchaseOrderItem.po_id == po_id)
        )
        if remaining:
            continue  # other orders' items still live in this shared PO
        cancelled.append(cancel_po(session, po))
    return cancelled
