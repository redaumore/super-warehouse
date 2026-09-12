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
    po_detail,
    receive_po_action,
    send_po_action,
)
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Inventory,
    ListaPrecios,
    Supplier,
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
        Supplier(id=1, code="MAY", business_name="Mayorista SA", default_margin_pct=Decimal(0))
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
    """The listing shows PO, supplier, state and items."""
    session = po_ctx["session"]
    rows = list_purchase_orders(session)
    assert len(rows) == 1
    assert rows[0]["po_id"] == po_ctx["po_id"]
    assert rows[0]["supplier"] == "Mayorista SA"
    assert rows[0]["estado"] == "OPEN"
    assert "CLV-001 × 10" in rows[0]["items"]


def test_send_open_po_moves_to_sent(po_ctx):
    """Sending from OPEN moves the PO to SENT."""
    session = po_ctx["session"]
    result = send_po_action(session, po_ctx["po_id"])
    assert "sent to the supplier" in result
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.SENT


def test_partial_then_full_receipt(po_ctx):
    """Receive partially then the rest: PARTIALLY_RECEIVED → FULLY_RECEIVED."""
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
    """Over-receiving is rejected and the PO does not mutate."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    with pytest.raises(ValueError, match="exceeds"):
        receive_po_action(session, po_ctx["po_id"], "CLV-001", 11)
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.SENT


def test_cancel_from_open(po_ctx):
    """Cancelling from OPEN moves the PO to CANCELLED."""
    session = po_ctx["session"]
    result = cancel_po_action(session, po_ctx["po_id"])
    assert "cancelado" in result
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.CANCELLED


def test_cancel_from_sent(po_ctx):
    """Cancelling from SENT is also valid."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    cancel_po_action(session, po_ctx["po_id"])
    assert _po(session, po_ctx["po_id"]).estado is SupplierPurchaseOrderState.CANCELLED


def test_cancel_terminal_po_is_rejected(po_ctx):
    """Cancelling a terminal PO (FULLY_RECEIVED) is rejected."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    receive_po_action(session, po_ctx["po_id"], "CLV-001", 10)
    with pytest.raises(Exception, match="cannot cancel"):
        cancel_po_action(session, po_ctx["po_id"])


def test_send_after_receiving_is_rejected(po_ctx):
    """Sending an already-received PO is rejected (state machine)."""
    session = po_ctx["session"]
    send_po_action(session, po_ctx["po_id"])
    receive_po_action(session, po_ctx["po_id"], "CLV-001", 10)
    with pytest.raises(Exception, match="cannot send"):
        send_po_action(session, po_ctx["po_id"])


# ------------------------------------------------------- PO detail (row click)


def test_po_detail_lists_lines_with_supplier_codes(po_ctx):
    """The detail resolves supplier codes and catalog names in batch."""
    session = po_ctx["session"]
    session.add(
        Catalogo(
            codigo_interno="CLV-001",
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal(0),
            precio_lista_base=Decimal("100.00"),
            sinonimos=[],
        )
    )
    from src.backoffice.sku_mappings import record_supplier_sku

    record_supplier_sku(session, 1, "CL-001-A", "CLV-001")
    session.add(
        SupplierPurchaseOrderItem(po_id=po_ctx["po_id"], sku="GONE-1", quantity=2, received_quantity=1)
    )
    session.flush()

    detail = po_detail(session, po_ctx["po_id"])
    assert detail["po_id"] == po_ctx["po_id"]
    assert detail["estado"] == "OPEN"
    assert detail["supplier"] == "Mayorista SA"
    lines = detail["lines"]
    assert [line["sku"] for line in lines] == ["CLV-001", "GONE-1"]
    assert lines[0]["codigo_proveedor"] == "CL-001-A"
    assert lines[0]["name"] == "Clavos Paris 2 Pulgadas"
    assert (lines[0]["quantity"], lines[0]["received_quantity"]) == (10, 0)
    # Delisted product: no mapping, no catalog row → graceful fallbacks.
    assert lines[1]["codigo_proveedor"] == ""
    assert lines[1]["name"] == "—"


def test_po_detail_unknown_po_raises(po_ctx):
    """An unknown PO id is a KeyError so the UI handler can clear the grid."""
    with pytest.raises(KeyError, match="unknown purchase order"):
        po_detail(po_ctx["session"], 9999)


def test_po_detail_empty_po_returns_no_lines(po_ctx):
    """A PO without lines renders an empty detail grid, not an error."""
    po = SupplierPurchaseOrder(supplier_id=1, estado=SupplierPurchaseOrderState.OPEN)
    po_ctx["session"].add(po)
    po_ctx["session"].flush()

    detail = po_detail(po_ctx["session"], po.po_id)
    assert detail["lines"] == []
