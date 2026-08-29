"""Backoffice purchase order view and execution (task 7.1).

Pure DB operations behind the Gradio "Purchase Orders" tab: list every
``SupplierPurchaseOrder`` with its state and lines, and let the owner execute
the lifecycle transitions (send → SENT, receive partial/full, cancel) wrapping
``src/purchasing/state.py``.

Unlike the catalog/clients handlers (which flush and rely on the caller's
transaction), these actions COMMIT: the tab must persist the owner's execution
even though the Gradio handler opens a short-lived ``SessionLocal`` that closes
(and would otherwise roll back) at the end of the with-block.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import SupplierPurchaseOrder
from src.purchasing.state import cancel_po, receive_po, send_po


def _po(session: Session, po_id: int) -> SupplierPurchaseOrder:
    po = session.get(SupplierPurchaseOrder, po_id)
    if po is None:
        raise KeyError(f"unknown purchase order: {po_id}")
    return po


def list_purchase_orders(session: Session) -> list[dict[str, object]]:
    """Every purchase order for the grid: id, supplier, state, lines, receipts."""
    rows = []
    for po in session.scalars(
        select(SupplierPurchaseOrder).order_by(SupplierPurchaseOrder.po_id.desc())
    ):
        supplier = po.supplier.business_name if po.supplier else str(po.supplier_id)
        items = "; ".join(f"{item.sku} × {item.quantity}" for item in po.items)
        received = "; ".join(f"{item.sku} × {item.received_quantity}" for item in po.items)
        rows.append(
            {
                "po_id": po.po_id,
                "supplier": supplier,
                "estado": po.estado.value,
                "items": items or "—",
                "received": received or "—",
            }
        )
    return rows


def send_po_action(session: Session, po_id: int) -> str:
    """Execute OPEN → SENT (owner sends the PO to the supplier)."""
    send_po(session, _po(session, po_id))
    session.commit()
    return f"PO #{po_id} sent to the supplier."


def receive_po_action(session: Session, po_id: int, sku: str, quantity: int) -> str:
    """Record a partial or full receipt for one SKU (bumps Inventory too)."""
    if not sku.strip():
        raise ValueError("indicá el SKU recibido")
    if quantity <= 0:
        raise ValueError("la cantidad recibida debe ser positiva")
    po = receive_po(session, _po(session, po_id), {sku.strip(): quantity})
    session.commit()
    return f"PO #{po_id}: recibidos {quantity} × {sku.strip()} → {po.estado.value}."


def cancel_po_action(session: Session, po_id: int) -> str:
    """Execute OPEN|SENT → CANCELLED."""
    cancel_po(session, _po(session, po_id))
    session.commit()
    return f"PO #{po_id} cancelado."
