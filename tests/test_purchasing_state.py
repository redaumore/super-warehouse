"""Supplier purchase order state machine tests (task 2.5).

Unit tests (no DB): every legal transition and every terminal rejection, using
a minimal fake session so no Postgres is required. Receiving bumps the
canonical Inventory; the fake serves inventory rows so the side effect is
asserted without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.db.models import (
    Inventory,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)
from src.purchasing.state import (
    InvalidTransitionError,
    cancel_po,
    receive_po,
    send_po,
)


class _FakeSession:
    """Minimal stand-in for the SQLAlchemy Session used by the transitions."""

    def __init__(self, inventory_row: Inventory | None = None):
        self.inventory_row = inventory_row
        self.added: list[object] = []
        self.flushed = 0

    def scalar(self, _statement):
        return self.inventory_row

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed += 1


def _po(estado: SupplierPurchaseOrderState = SupplierPurchaseOrderState.OPEN) -> SupplierPurchaseOrder:
    po = SupplierPurchaseOrder(
        po_id=1,
        supplier_id=10,
        estado=estado,
    )
    po.items = [
        SupplierPurchaseOrderItem(
            po_item_id=1, po_id=1, sku="CLV-001", quantity=10, received_quantity=0
        )
    ]
    return po


def test_send_open_po_moves_to_sent():
    """Enviar un PO abierto lo mueve a Enviado."""
    session = _FakeSession()
    po = _po()
    send_po(session, po)
    assert po.estado is SupplierPurchaseOrderState.SENT
    assert po.sent_at is not None
    assert session.flushed >= 1


def test_send_non_open_po_is_invalid():
    """Enviar un PO que no está abierto es una transición inválida."""
    for estado in (
        SupplierPurchaseOrderState.SENT,
        SupplierPurchaseOrderState.PARTIALLY_RECEIVED,
        SupplierPurchaseOrderState.FULLY_RECEIVED,
        SupplierPurchaseOrderState.CANCELLED,
    ):
        with pytest.raises(InvalidTransitionError, match="cannot send"):
            send_po(_FakeSession(), _po(estado))


def test_cancel_open_po_moves_to_cancelled():
    """Cancelar un PO abierto lo mueve a Cancelado."""
    session = _FakeSession()
    po = _po()
    cancel_po(session, po)
    assert po.estado is SupplierPurchaseOrderState.CANCELLED
    assert po.cancelled_at is not None


def test_cancel_sent_po_moves_to_cancelled():
    """Cancelar un PO enviado también es válido."""
    session = _FakeSession()
    po = _po(SupplierPurchaseOrderState.SENT)
    cancel_po(session, po)
    assert po.estado is SupplierPurchaseOrderState.CANCELLED


def test_cancel_terminal_po_is_invalid():
    """Cancelar un PO terminal (recibido o cancelado) es inválido."""
    for estado in (
        SupplierPurchaseOrderState.FULLY_RECEIVED,
        SupplierPurchaseOrderState.CANCELLED,
    ):
        with pytest.raises(InvalidTransitionError, match="cannot cancel"):
            cancel_po(_FakeSession(), _po(estado))


def test_receive_partial_sent_po_moves_to_partially_received():
    """Recibir parcialmente un PO enviado lo mueve a Parcialmente Recibido."""
    inventory = Inventory(sku_id="CLV-001", quantity_on_hand=0)
    session = _FakeSession(inventory_row=inventory)
    po = _po(SupplierPurchaseOrderState.SENT)
    receive_po(session, po, {"CLV-001": 4})
    assert po.estado is SupplierPurchaseOrderState.PARTIALLY_RECEIVED
    assert po.items[0].received_quantity == 4
    assert inventory.quantity_on_hand == 4  # supplier goods arrived into stock


def test_receive_full_sent_po_moves_to_fully_received():
    """Recibir todo el PO lo mueve a Completamente Recibido (terminal)."""
    inventory = Inventory(sku_id="CLV-001", quantity_on_hand=0)
    session = _FakeSession(inventory_row=inventory)
    po = _po(SupplierPurchaseOrderState.SENT)
    receive_po(session, po, {"CLV-001": 10})
    assert po.estado is SupplierPurchaseOrderState.FULLY_RECEIVED
    assert po.items[0].received_quantity == 10
    assert inventory.quantity_on_hand == 10


def test_receive_completes_from_partially_received():
    """Completar la recepción desde Parcialmente Recibido llega a Recibido."""
    inventory = Inventory(sku_id="CLV-001", quantity_on_hand=4)
    session = _FakeSession(inventory_row=inventory)
    po = _po(SupplierPurchaseOrderState.PARTIALLY_RECEIVED)
    po.items[0].received_quantity = 4
    receive_po(session, po, {"CLV-001": 6})
    assert po.estado is SupplierPurchaseOrderState.FULLY_RECEIVED
    assert inventory.quantity_on_hand == 10


def test_receive_over_remaining_quantity_raises():
    """Recibir más de lo pendiente se rechaza sin mutar el PO."""
    inventory = Inventory(sku_id="CLV-001", quantity_on_hand=0)
    session = _FakeSession(inventory_row=inventory)
    po = _po(SupplierPurchaseOrderState.SENT)
    with pytest.raises(ValueError, match="exceeds"):
        receive_po(session, po, {"CLV-001": 11})
    assert po.estado is SupplierPurchaseOrderState.SENT
    assert po.items[0].received_quantity == 0


def test_receive_unknown_sku_raises():
    """Recibir un SKU que no está en el PO se rechaza."""
    session = _FakeSession()
    po = _po(SupplierPurchaseOrderState.SENT)
    with pytest.raises(KeyError, match="not in purchase order"):
        receive_po(session, po, {"OTRO-9": 1})


def test_receive_from_terminal_po_is_invalid():
    """Recibir un PO terminal es inválido."""
    for estado in (
        SupplierPurchaseOrderState.OPEN,
        SupplierPurchaseOrderState.FULLY_RECEIVED,
        SupplierPurchaseOrderState.CANCELLED,
    ):
        with pytest.raises(InvalidTransitionError, match="cannot receive"):
            receive_po(_FakeSession(), _po(estado), {"CLV-001": 1})


def test_receive_creates_inventory_row_when_absent():
    """Recibir sin fila de Inventory crea la fila con lo recibido."""
    session = _FakeSession(inventory_row=None)
    po = _po(SupplierPurchaseOrderState.SENT)
    receive_po(session, po, {"CLV-001": 3})
    assert any(
        isinstance(obj, Inventory) and obj.quantity_on_hand == 3 for obj in session.added
    )
    assert po.estado is SupplierPurchaseOrderState.PARTIALLY_RECEIVED


def test_receive_touches_updated_at():
    """Recibir refresca updated_at de Inventory."""
    inventory = Inventory(sku_id="CLV-001", quantity_on_hand=0)
    session = _FakeSession(inventory_row=inventory)
    reference = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    po = _po(SupplierPurchaseOrderState.SENT)
    receive_po(session, po, {"CLV-001": 2}, now=reference)
    assert inventory.updated_at == reference