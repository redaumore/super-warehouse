"""RED tests for Phase 1 (PR1): data model + migration.

Prove the ORM models encode the design's entities and that the committed
Alembic migration produces those tables with the `vector(1536)` column and the
pgvector extension.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from src.agents.customer import _default_margin
from src.backoffice.customer_orders import get_default_margin
from src.db.models import (
    Base,
    Catalogo,
    Cliente,
    IvaCondition,
    ListaPrecios,
    Order,
    OrderEstado,
    SourcingState,
    Supplier,
    SupplierPurchaseOrderState,
    SupplierStatus,
)
from src.pricing.order_pricing import PricingLine, compute_order


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
    date; the six order states remain untouched.
    """
    cols = Order.__table__.c
    assert "sourcing_state" in cols
    assert "delivery_date" in cols
    assert "estado" in cols  # the six-state machine is still there
    assert {m.value for m in OrderEstado} == {
        "DRAFT",
        "CONFIRMED",
        "PICKING",
        "READY_FOR_DELIVERY",
        "CANCELED",
        "CLOSED",
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
    """La máquina de estados del pedido se fija a los seis estados de la spec.

    The order state machine is fixed to the six spec states.
    """
    values = {m.value for m in OrderEstado}
    assert values == {
        "DRAFT",
        "CONFIRMED",
        "PICKING",
        "READY_FOR_DELIVERY",
        "CANCELED",
        "CLOSED",
    }


def test_order_has_one_draft_per_customer_partial_index():
    """El modelo orders declara el índice único parcial de un draft por cliente.

    The Order model declares the partial unique index (one Draft per customer,
    AD4) that migration f2b2570aed04 creates.
    """
    indexes = {index.name: index for index in Order.__table__.indexes}
    assert "uq_orders_one_draft_per_customer" in indexes
    assert indexes["uq_orders_one_draft_per_customer"].unique is True


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


def test_migration_order_estado_has_six_values(db_inspector):
    """La migración deja el enum order_estado con los seis estados de la spec.

    The migrated order_estado enum carries exactly the six spec states
    (task 1.3: six enum values via db_inspector).
    """
    enums = {e["name"]: e for e in db_inspector.get_enums()}
    assert "order_estado" in enums
    assert set(enums["order_estado"]["labels"]) == {
        "DRAFT",
        "CONFIRMED",
        "PICKING",
        "READY_FOR_DELIVERY",
        "CANCELED",
        "CLOSED",
    }


def test_migration_creates_one_draft_per_customer_index(db_inspector):
    """La migración crea el índice único parcial de un draft por cliente."""
    indexes = {i["name"]: i for i in db_inspector.get_indexes("orders")}
    assert "uq_orders_one_draft_per_customer" in indexes
    assert indexes["uq_orders_one_draft_per_customer"]["unique"] is True
    where = indexes["uq_orders_one_draft_per_customer"]["dialect_options"]["postgresql_where"]
    assert "DRAFT" in where


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


def test_order_state_machine_migration_downgrade_safety(db_engine, clean_schema):
    """The order-state-machine migration downgrades safely and re-upgrades.

    One-step downgrade to 7d2f4a1e8b90 reconciles DRAFT/PICKING rows to the
    legacy equivalents, drops the partial index, and leaves the two extra enum
    labels behind (PG cannot drop enum values). The guarded re-upgrade then
    succeeds (task 1.3: downgrade safety).

    NOTE: the previous deep round-trip (downgrade to 46bdbdc4a575 + Case A
    persist asserting OrderEstado.PENDING_APPROVAL) was replaced: that test
    asserted a removed enum member and exercised a legacy write path that
    Phase 3 rewrites to persist DRAFT.
    """
    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = AlembicConfig(str(alembic_ini))

    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    with Session() as session:
        session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
        session.add(
            Supplier(
                id=1,
                code="DWG",
                business_name="Downgrade Supplier",
                default_margin_pct=Decimal(0),
            )
        )
        for customer_id, phone in ((1, "+5491155551111"), (2, "+5491155552222")):
            session.add(
                Cliente(
                    customer_id=customer_id,
                    nombre_comercial=f"Downgrade Customer {customer_id}",
                    telefono_norm=phone,
                    lista_precios_id=1,
                    descuento_particular_pct=Decimal(0),
                )
            )
        session.flush()
        session.add_all(
            [
                Order(customer_id=1, estado=OrderEstado.DRAFT),
                Order(customer_id=2, estado=OrderEstado.PICKING),
            ]
        )
        session.commit()

    command.downgrade(config, "7d2f4a1e8b90")
    try:
        inspector = inspect(db_engine)
        # The partial unique index is gone.
        indexes = {i["name"] for i in inspector.get_indexes("orders")}
        assert "uq_orders_one_draft_per_customer" not in indexes
        # DRAFT/PICKING labels remain (PG cannot drop enum values)...
        enums = {e["name"]: e for e in inspector.get_enums()}
        labels = set(enums["order_estado"]["labels"])
        assert {"DRAFT", "PICKING"}.issubset(labels)
        # ...while the four legacy states are usable again.
        assert {
            "PENDING_APPROVAL",
            "APPROVED",
            "IN_DISPATCH",
            "REJECTED",
        }.issubset(labels)
        with db_engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text("SELECT order_id, estado FROM orders ORDER BY order_id")
                ).all()
            )
        # DRAFT -> CONFIRMED -> PENDING_APPROVAL; PICKING -> READY_FOR_DELIVERY
        # -> APPROVED (reconciled before the reverse renames).
        assert rows[1] == "PENDING_APPROVAL"
        assert rows[2] == "APPROVED"
    finally:
        # Guarded re-upgrade: leftover labels must not collide with ADD VALUE.
        command.upgrade(config, "head")


def test_migration_seeded_default_margin_is_read_by_pricing(db_engine, clean_schema):
    """A freshly migrated DB seeds default_margin_pct=20 and pricing consumes it."""
    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = AlembicConfig(str(alembic_ini))
    previous_revision = "46bdbdc4a575"

    command.downgrade(config, previous_revision)
    command.upgrade(config, "head")
    try:
        Session = sessionmaker(bind=db_engine, expire_on_commit=False)
        with Session() as session:
            # First read on the fresh database must see the seeded 20%, with no
            # test-inserted row involved.
            assert get_default_margin(session) == Decimal(20)
            assert _default_margin(session) == Decimal("0.20")

            # The code path that applies it: an unmapped RAG supplier falls back
            # to the seeded default, so 100.00 → 120.00.
            priced = compute_order(
                (
                    PricingLine(
                        sku="RAG-UNMAPPED-1",
                        cantidad=1,
                        source="RAG",
                        name="RAG item",
                        price=Decimal("100.00"),
                        currency="ARS",
                        supplier="UNMAPPED",
                        codigo_proveedor="UNMAPPED",
                    ),
                ),
                supplier_margin=lambda code: None,
                default_margin=_default_margin(session),
            )
            assert priced.lines[0].base_ars == Decimal("120.00")
    finally:
        command.upgrade(config, "head")
