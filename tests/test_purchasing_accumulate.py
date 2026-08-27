"""Purchase order accumulation tests (task 2.6).

Integration (Postgres, skipped when down): a later customer order merges into
the supplier's existing OPEN PO (items aggregate by SKU), several suppliers
produce one PO each, and an owner re-selection before execution detaches the
need from the previous OPEN PO so the SKU is never double-ordered.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.config import get_settings
from src.db.models import (
    Cliente,
    ListaPrecios,
    Order,
    Proveedor,
    SourcingNeed,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)
from src.purchasing.accumulate import accumulate_need, open_or_create_po


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
def suppliers(db_session):
    """Two suppliers plus a customer and two orders for the needs."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(Cliente(customer_id=1, nombre_comercial="Cliente Test", telefono_norm="+5491155559999", lista_precios_id=1, descuento_particular_pct=Decimal(0)))
    db_session.add(Proveedor(proveedor_id=1, razon_social="Proveedor X", margen_predeterminado=Decimal(0)))
    db_session.add(Proveedor(proveedor_id=2, razon_social="Proveedor Y", margen_predeterminado=Decimal(0)))
    db_session.add(Order(order_id=100, customer_id=1))
    db_session.add(Order(order_id=200, customer_id=1))
    db_session.flush()
    return db_session


def _need(session, order_id, sku, missing, *, supplier_id=None, po_item_id=None) -> SourcingNeed:
    need = SourcingNeed(
        order_id=order_id,
        sku=sku,
        missing_quantity=missing,
        supplier_id=supplier_id,
        po_item_id=po_item_id,
    )
    session.add(need)
    session.flush()
    return need


def test_open_or_create_po_reuses_existing_open_po(suppliers):
    """open_or_create_po devuelve el mismo PO abierto del proveedor."""
    first = open_or_create_po(suppliers, 1)
    second = open_or_create_po(suppliers, 1)
    assert second.po_id == first.po_id
    assert second.estado is SupplierPurchaseOrderState.OPEN


def test_open_or_create_po_splits_by_supplier(suppliers):
    """Cada proveedor tiene su propio PO abierto."""
    po_x = open_or_create_po(suppliers, 1)
    po_y = open_or_create_po(suppliers, 2)
    assert po_x.po_id != po_y.po_id


def test_second_order_merges_into_existing_open_po(suppliers):
    """Un segundo pedido se acumula en el PO abierto existente del proveedor."""
    order_a = 100
    order_b = 200
    need_a = _need(suppliers, order_a, "CLV-001", 6)
    need_b = _need(suppliers, order_b, "CLV-001", 4)

    po_a = accumulate_need(suppliers, need_a, supplier_id=1)
    po_b = accumulate_need(suppliers, need_b, supplier_id=1)

    assert po_b.po_id == po_a.po_id  # same supplier → same OPEN PO
    items = suppliers.scalars(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po_a.po_id
        )
    ).all()
    assert len(items) == 1  # aggregated by SKU, no duplicate rows
    assert items[0].sku == "CLV-001"
    assert items[0].quantity == 10  # 6 + 4
    # Both needs point at the shared PO item.
    assert suppliers.get(SourcingNeed, need_a.need_id).po_item_id == items[0].po_item_id
    assert suppliers.get(SourcingNeed, need_b.need_id).po_item_id == items[0].po_item_id


def test_new_sku_appends_a_line_to_the_same_po(suppliers):
    """Un SKU nuevo agrega una línea al mismo PO, sin duplicar el PO."""
    need_a = _need(suppliers, 100, "CLV-001", 5)
    need_b = _need(suppliers, 200, "TRN-002", 3)
    po_a = accumulate_need(suppliers, need_a, supplier_id=1)
    po_b = accumulate_need(suppliers, need_b, supplier_id=1)
    assert po_b.po_id == po_a.po_id
    items = suppliers.scalars(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po_a.po_id
        )
    ).all()
    assert {i.sku for i in items} == {"CLV-001", "TRN-002"}


def test_multiple_suppliers_produce_multiple_pos(suppliers):
    """Seleccionar proveedores X e Y genera un PO para cada uno."""
    need_x = _need(suppliers, 100, "CLV-001", 6)
    need_y = _need(suppliers, 100, "PINT-001", 2)

    po_x = accumulate_need(suppliers, need_x, supplier_id=1)
    po_y = accumulate_need(suppliers, need_y, supplier_id=2)

    assert po_x.po_id != po_y.po_id
    assert po_x.supplier_id == 1
    assert po_y.supplier_id == 2
    item_x = suppliers.scalar(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po_x.po_id
        )
    )
    item_y = suppliers.scalar(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po_y.po_id
        )
    )
    assert item_x.sku == "CLV-001"
    assert item_y.sku == "PINT-001"


def test_reselection_detaches_from_previous_open_po(suppliers):
    """Re-elegir proveedor antes de ejecutar mueve la necesidad al nuevo PO."""
    need = _need(suppliers, 100, "CLV-001", 6)
    po_x = accumulate_need(suppliers, need, supplier_id=1)
    # Owner changes their mind before the PO is sent.
    po_y = accumulate_need(suppliers, need, supplier_id=2)

    assert need.supplier_id == 2
    assert po_y.po_id != po_x.po_id
    # The old PO's line lost the quantity (removed → no rows left).
    old_items = suppliers.scalars(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po_x.po_id
        )
    ).all()
    assert old_items == []
    new_items = suppliers.scalars(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po_y.po_id
        )
    ).all()
    assert len(new_items) == 1
    assert new_items[0].quantity == 6
    assert need.po_item_id == new_items[0].po_item_id


def test_reselection_keeps_linked_item_when_quantity_remains(suppliers):
    """Si al desvincular queda cantidad en la línea vieja, la línea sobrevive."""
    need_a = _need(suppliers, 100, "CLV-001", 6)
    need_b = _need(suppliers, 200, "CLV-001", 4)
    accumulate_need(suppliers, need_a, supplier_id=1)
    po_x = accumulate_need(suppliers, need_b, supplier_id=1)
    # need_a (6) stays with X; need_b (4) moves to Y.
    po_y = accumulate_need(suppliers, need_b, supplier_id=2)

    assert po_y.po_id != po_x.po_id
    old_line = suppliers.scalar(
        select(SupplierPurchaseOrderItem).where(
            SupplierPurchaseOrderItem.po_id == po_x.po_id
        )
    )
    assert old_line is not None
    assert old_line.quantity == 6  # need_a's share remains
    assert suppliers.get(SourcingNeed, need_a.need_id).supplier_id == 1
    assert suppliers.get(SourcingNeed, need_b.need_id).supplier_id == 2