"""RED tests for Phase 1 (PR1): data model + migration.

Prove the ORM models encode the design's entities and that the committed
Alembic migration produces those tables with the `vector(1536)` column and the
pgvector extension.
"""

from __future__ import annotations

from sqlalchemy import text

from src.db.models import Base, Catalogo, Cliente, OrderEstado


def test_all_design_entities_are_modeled():
    """Every design entity has a corresponding ORM model."""
    tables = set(Base.metadata.tables)
    expected = {
        "lista_precios",
        "clientes",
        "catalogo",
        "proveedores",
        "proveedor_sku_mapping",
        "stock_reservations",
        "orders",
        "order_items",
    }
    assert expected.issubset(tables)


def test_catalogo_has_vector_1536_embedding():
    """`catalogo.embedding` is declared as a pgvector vector(1536)."""
    col = Catalogo.__table__.c["embedding"]
    assert col.type.__class__.__name__ == "VECTOR"
    assert col.type.dim == 1536


def test_cliente_has_no_credit_or_payment_fields():
    """Per spec, `clientes` MUST NOT model credit limits / payment conditions."""
    cols = set(Cliente.__table__.c.keys())
    assert not (cols & {"credito", "limite_credito", "condiciones_pago", "payment"})


def test_order_estado_enum_values():
    """The order state machine is fixed to the four spec states."""
    values = {m.value for m in OrderEstado}
    assert values == {"PENDING_APPROVAL", "APPROVED", "IN_DISPATCH", "REJECTED"}


def test_migration_creates_all_tables(db_inspector):
    """The migrated schema contains every design table."""
    tables = set(db_inspector.get_table_names())
    expected = {
        "lista_precios",
        "clientes",
        "catalogo",
        "proveedores",
        "proveedor_sku_mapping",
        "stock_reservations",
        "orders",
        "order_items",
    }
    assert expected.issubset(tables)


def test_migration_has_vector_1536_column(db_inspector):
    """The migrated `catalogo.embedding` column is vector(1536)."""
    col = next(c for c in db_inspector.get_columns("catalogo") if c["name"] == "embedding")
    assert col["type"].__class__.__name__ == "VECTOR"
    assert col["type"].dim == 1536


def test_migration_enables_pgvector_extension(db_engine):
    """The pgvector extension is installed in the migrated schema."""
    with db_engine.connect() as conn:
        row = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar()
    assert row == 1
