"""RED tests for Phase 1 (PR1): data model + migration.

Prove the ORM models encode the design's entities and that the committed
Alembic migration produces those tables with the `vector(1536)` column and the
pgvector extension.
"""

from __future__ import annotations

from sqlalchemy import text

from src.db.models import (
    Base,
    Catalogo,
    Cliente,
    IvaCondition,
    Order,
    OrderEstado,
    SourcingState,
    Supplier,
    SupplierPurchaseOrderState,
    SupplierStatus,
)


def test_all_design_entities_are_modeled():
    """Cada entidad del diseño tiene su modelo ORM correspondiente.

    Every design entity has a corresponding ORM model.
    """
    tables = set(Base.metadata.tables)
    expected = {
        "lista_precios",
        "clientes",
        "catalogo",
        "suppliers",
        "supplier_sku_mappings",
        "stock_reservations",
        "orders",
        "order_items",
        "inventory",
        "supplier_purchase_orders",
        "supplier_purchase_order_items",
        "sourcing_needs",
    }
    assert expected.issubset(tables)


def test_sourcing_entities_are_modeled():
    """Las tablas del eje de sourcing existen en el modelo ORM.

    The sourcing axis tables (Inventory, POs, SourcingNeed) are modeled.
    """
    tables = set(Base.metadata.tables)
    assert {
        "inventory",
        "supplier_purchase_orders",
        "supplier_purchase_order_items",
        "sourcing_needs",
    }.issubset(tables)


def test_order_has_sourcing_axis_and_delivery_date():
    """El pedido tiene sourcing_state y delivery_date, sin tocar order_estado.

    Order carries the separate sourcing axis and the informational delivery
    date; the four approval states remain untouched.
    """
    cols = Order.__table__.c
    assert "sourcing_state" in cols
    assert "delivery_date" in cols
    assert "estado" in cols  # the four-state machine is still there
    assert {m.value for m in OrderEstado} == {
        "PENDING_APPROVAL",
        "APPROVED",
        "IN_DISPATCH",
        "REJECTED",
    }


def test_sourcing_state_enum_values():
    """El enum SourcingState tiene exactamente los tres estados del eje."""
    assert {m.value for m in SourcingState} == {
        "PENDING_ASSEMBLY",
        "IN_PREPARATION",
        "CANCELLED",
    }


def test_po_state_enum_values():
    """El enum del PO tiene exactamente los cinco estados de su máquina."""
    assert {m.value for m in SupplierPurchaseOrderState} == {
        "OPEN",
        "SENT",
        "PARTIALLY_RECEIVED",
        "FULLY_RECEIVED",
        "CANCELLED",
    }


def test_supplier_status_enum_values():
    """El enum SupplierStatus tiene exactamente ACTIVO e INACTIVO."""
    assert {m.value for m in SupplierStatus} == {"ACTIVO", "INACTIVO"}


def test_iva_condition_enum_values():
    """El enum IvaCondition tiene exactamente los cinco valores confirmados."""
    assert {m.value for m in IvaCondition} == {
        "RESPONSABLE_INSCRIPTO",
        "MONOTRIBUTO",
        "EXENTO",
        "CONSUMIDOR_FINAL",
        "NO_RESPONSABLE",
    }


def test_supplier_model_has_master_data_columns():
    """El modelo suppliers expone las columnas de datos maestros."""
    cols = set(Supplier.__table__.c.keys())
    assert {
        "id",
        "business_name",
        "contact_name",
        "phone",
        "default_margin_pct",
        "terms",
        "cuit",
        "address",
        "email",
        "whatsapp",
        "code",
        "iva_condition",
        "status",
    }.issubset(cols)


def test_supplier_code_and_cuit_indexes():
    """code tiene índice único; cuit único parcial cuando no es NULL."""
    indexes = {index.name: index for index in Supplier.__table__.indexes}
    assert "uq_suppliers_code" in indexes
    assert indexes["uq_suppliers_code"].unique is True
    assert "uq_suppliers_cuit" in indexes
    assert indexes["uq_suppliers_cuit"].unique is True


def test_catalogo_has_vector_1536_embedding():
    """La columna `catalogo.embedding` se declara como pgvector vector(1536).

    `catalogo.embedding` is declared as a pgvector vector(1536).
    """
    col = Catalogo.__table__.c["embedding"]
    assert col.type.__class__.__name__ == "VECTOR"
    assert col.type.dim == 1536


def test_cliente_has_no_credit_or_payment_fields():
    """El modelo `clientes` no modela límites de crédito ni condiciones de pago.

    Per spec, `clientes` MUST NOT model credit limits / payment conditions.
    """
    cols = set(Cliente.__table__.c.keys())
    assert not (cols & {"credito", "limite_credito", "condiciones_pago", "payment"})


def test_order_estado_enum_values():
    """La máquina de estados del pedido se fija a los cuatro estados de la spec.

    The order state machine is fixed to the four spec states.
    """
    values = {m.value for m in OrderEstado}
    assert values == {"PENDING_APPROVAL", "APPROVED", "IN_DISPATCH", "REJECTED"}


def test_migration_creates_all_tables(db_inspector):
    """La migración crea todas las tablas del diseño.

    The migrated schema contains every design table.
    """
    tables = set(db_inspector.get_table_names())
    expected = {
        "lista_precios",
        "clientes",
        "catalogo",
        "suppliers",
        "supplier_sku_mappings",
        "stock_reservations",
        "orders",
        "order_items",
        "inventory",
        "supplier_purchase_orders",
        "supplier_purchase_order_items",
        "sourcing_needs",
    }
    assert expected.issubset(tables)


def test_migration_creates_sourcing_columns(db_inspector):
    """RED: la migración agrega sourcing_state y delivery_date a orders.

    The migration adds the sourcing axis + delivery_date columns to orders.
    """
    cols = {c["name"] for c in db_inspector.get_columns("orders")}
    assert {"sourcing_state", "delivery_date"}.issubset(cols)


def test_migration_creates_sourcing_enums(db_inspector):
    """RED: los enums del eje de sourcing existen tras la migración."""
    enums = {e["name"] for e in db_inspector.get_enums()}
    assert {"sourcing_state", "supplier_purchase_order_state"}.issubset(enums)


def test_migration_indexes_sourcing_needs(db_inspector):
    """RED: sourcing_needs queda indexado por order_id y supplier_id."""
    indexes = {i["name"] for i in db_inspector.get_indexes("sourcing_needs")}
    assert {"ix_sourcing_needs_order_id", "ix_sourcing_needs_supplier_id"}.issubset(indexes)


def test_migration_has_vector_1536_column(db_inspector):
    """La columna migrada `catalogo.embedding` es vector(1536).

    The migrated `catalogo.embedding` column is vector(1536).
    """
    col = next(c for c in db_inspector.get_columns("catalogo") if c["name"] == "embedding")
    assert col["type"].__class__.__name__ == "VECTOR"
    assert col["type"].dim == 1536


def test_migration_enables_pgvector_extension(db_engine):
    """La extensión pgvector queda instalada en el esquema migrado.

    The pgvector extension is installed in the migrated schema.
    """
    with db_engine.connect() as conn:
        row = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar()
    assert row == 1
