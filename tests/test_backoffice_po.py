"""Backoffice purchase order execution tests (task 7.3).

Integration: the PO lifecycle driven through the backoffice actions —
OPEN → SENT → PARTIALLY_RECEIVED → FULLY_RECEIVED and CANCELLED from OPEN and
SENT — plus the listing and the Inventory bump on receipt.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.backoffice.po import (
    cancel_po_action,
    list_purchase_orders,
    receive_po_action,
    send_po_action,
)
from src.config import get_settings
from src.db.models import (
    Inventory,
    ListaPrecios,
    Proveedor,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)


def _postgres_up() -> bool:
    try:
        engine = create_engine(
            get_settings().sqlalchemy_database_url, connect_args={"connect_timeout": 2}
        )
        with engine.connect():
            pass
        engine.dispose()
        return True
    except (OperationalError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _postgres_up(), reason="Postgres not running (make db-up)")


@pytest.fixture(autouse=True)
def _clean_schema(clean_schema):
    yield


@pytest.fixture
def po_ctx(db_session):
    """A supplier and an OPEN purchase order with one line of 10 units."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Proveedor(
            proveedor_id=1, razon_social="Proveedor Mayorista", margen_predeterminado=Decimal(0)
        )
    )
    po = SupplierPurchaseOrder(supplier_id=1, estado=SupplierPurchaseOrderState.OPEN)
    db_session.add(po)
    db_session.flush()
    db_session.add(
        SupplierPurchaseOrderItem(po_id=po.po_id, sku="CLV-001", quantity=10, received_quantity=0)
    )
    db_session.flush()
    return {"session": db_session, "po_id": po.po_id}


def _po(session, po_id: int) -> SupplierPurchaseOrder:
    return session.get(SupplierPurchaseOrder, po_id)


def test_list_purchase_orders_renders_state_and_items(po_ctx):
    """El listado muestra PO, proveedor, estado y artículos."""
    session = po_ctx["session"]
    rows = list_purchase_orders(session)
    assert len(rows) == 1
    assert rows[0]["po_id"] == po_ctx["po_id"]
    assert rows[0]["supplier"] == "Proveedor Mayorista"
    assert rows[0]["estado"] == "OPEN"
    assert "CLV-001 × 10" in rows[0]["items"]


def test_send_open_po_moves_to_sent(po_ctx):
    """Enviar desde OPEN mueve el PO a SENT."""
    session = po_ctx["session"]
    result = send_po_action(session, po_ctx["po_id"])
    assert "enviado" in result
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.SENT


def test_partial_then_full_receipt(po_ctx):
    """Recibir parcial y luego el resto: PARTIALLY_RECEIVED → FULLY_RECEIVED."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])

    first = receive_po_action(session, po_ctx["po_id"], "CLV-001", 4)
    assert "PARTIALLY_RECEIVED" in first
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.PARTIALLY_RECEIVED
    # The supplier goods arrived into the canonical Inventory.
    on_hand = session.scalar(select(Inventory).where(Inventory.sku_id == "CLV-001"))
    assert on_hand.quantity_on_hand == 4

    second = receive_po_action(session, po_ctx["po_id"], "CLV-001", 6)
    assert "FULLY_RECEIVED" in second
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.FULLY_RECEIVED
    assert (
        session.scalar(select(Inventory).where(Inventory.sku_id == "CLV-001")).quantity_on_hand
        == 10
    )


def test_receive_more_than_remaining_is_rejected(po_ctx):
    """Recibir de más se rechaza y el PO no muta."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    with pytest.raises(ValueError, match="exceeds"):
        receive_po_action(session, po_ctx["po_id"], "CLV-001", 11)
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.SENT


def test_cancel_from_open(po_ctx):
    """Cancelar desde OPEN mueve el PO a CANCELLED."""
    session = po_ctx["session"]
    result = cancel_po_action(session, po_ctx["po_id"])
    assert "cancelado" in result
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.CANCELLED


def test_cancel_from_sent(po_ctx):
    """Cancelar desde SENT también es válido."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    cancel_po_action(session, po_ctx["po_id"])
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.CANCELLED


def test_cancel_terminal_po_is_rejected(po_ctx):
    """Cancelar un PO terminal (FULLY_RECEIVED) se rechaza."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    receive_po_action(session, po_ctx["po_id"], "CLV-001", 10)
    with pytest.raises(Exception, match="cannot cancel"):
        cancel_po_action(session, po_ctx["po_id"])


def test_send_after_receiving_is_rejected(po_ctx):
    """Enviar un PO ya recibido se rechaza (máquina de estados)."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    receive_po_action(session, po_ctx["po_id"], "CLV-001", 10)
    with pytest.raises(Exception, match="cannot send"):
        send_po_action(session, po_ctx["po_id"])
