"""supplier management

Renames the supplier domain to English and adds master-data columns.

Order (per design):
1. DELETE legacy supplier rows and dependents, child-first (all suppliers are
   legacy — user-approved deletion, no backfill, no preservation).
2. Rename ``proveedores``→``suppliers`` (PK ``proveedor_id``→``id``) and its
   columns; rename child FK columns and ``proveedor_sku_mapping``→
   ``supplier_sku_mappings`` with its columns. Postgres auto-rewrites FK
   definitions to follow the parent renames.
3. Add the master-data columns (``cuit``, ``address``, ``email``, ``whatsapp``,
   ``code``, ``iva_condition``, ``status``), the ``supplier_status`` /
   ``iva_condition`` enum types, the unique ``code`` index and the partial
   unique ``cuit`` index.

``downgrade`` restores the schema (renames back, drops the added columns and
types). Deleted rows are unrecoverable (user-approved).

Revision ID: 46bdbdc4a575
Revises: 5f304e18a765
Create Date: 2026-08-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '46bdbdc4a575'
down_revision: Union[str, None] = '5f304e18a765'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

supplier_status = postgresql.ENUM(
    'ACTIVO', 'INACTIVO', name='supplier_status', create_type=False
)
iva_condition = postgresql.ENUM(
    'RESPONSABLE_INSCRIPTO',
    'MONOTRIBUTO',
    'EXENTO',
    'CONSUMIDOR_FINAL',
    'NO_RESPONSABLE',
    name='iva_condition',
    create_type=False,
)


def upgrade() -> None:
    # 1. Delete legacy supplier rows and their dependents, child-first.
    #    (inventory/orders/order_items/stock_reservations/stock_adjustments
    #    reference SKUs by bare string — no FK — left untouched.)
    op.execute('DELETE FROM sourcing_needs WHERE supplier_id IS NOT NULL')
    op.execute('DELETE FROM supplier_purchase_order_items')
    op.execute('DELETE FROM supplier_purchase_orders')
    op.execute('DELETE FROM catalogo')
    op.execute('DELETE FROM proveedor_sku_mapping')
    op.execute('DELETE FROM proveedores')

    # 2. Rename tables/columns (FKs follow the parent renames automatically).
    op.rename_table('proveedores', 'suppliers')
    op.alter_column('suppliers', 'proveedor_id', new_column_name='id')
    op.alter_column('suppliers', 'razon_social', new_column_name='business_name')
    op.alter_column('suppliers', 'contacto', new_column_name='contact_name')
    op.alter_column('suppliers', 'telefono', new_column_name='phone')
    op.alter_column('suppliers', 'margen_predeterminado', new_column_name='default_margin_pct')
    op.alter_column('suppliers', 'condiciones', new_column_name='terms')
    op.alter_column('catalogo', 'proveedor_id', new_column_name='supplier_id')
    op.rename_table('proveedor_sku_mapping', 'supplier_sku_mappings')
    op.alter_column('supplier_sku_mappings', 'mapping_id', new_column_name='id')
    op.alter_column('supplier_sku_mappings', 'proveedor_id', new_column_name='supplier_id')
    op.alter_column(
        'supplier_sku_mappings', 'codigo_proveedor', new_column_name='supplier_sku_code'
    )
    op.alter_column(
        'supplier_sku_mappings', 'descripcion_raw', new_column_name='raw_description'
    )
    op.alter_column('supplier_sku_mappings', 'sku_interno', new_column_name='internal_sku')
    op.alter_column('supplier_sku_mappings', 'confianza', new_column_name='confidence')

    # 3. Enum types + master-data columns + indexes.
    supplier_status.create(op.get_bind(), checkfirst=True)
    iva_condition.create(op.get_bind(), checkfirst=True)
    op.add_column('suppliers', sa.Column('cuit', sa.String(length=13), nullable=True))
    op.add_column('suppliers', sa.Column('address', sa.String(length=300), nullable=True))
    op.add_column('suppliers', sa.Column('email', sa.String(length=254), nullable=True))
    op.add_column('suppliers', sa.Column('whatsapp', sa.String(length=32), nullable=True))
    op.add_column('suppliers', sa.Column('code', sa.String(length=3), nullable=False))
    op.add_column(
        'suppliers',
        sa.Column('iva_condition', iva_condition, nullable=True),
    )
    op.add_column(
        'suppliers',
        sa.Column('status', supplier_status, server_default='ACTIVO', nullable=False),
    )
    op.create_index('uq_suppliers_code', 'suppliers', ['code'], unique=True)
    op.create_index(
        'uq_suppliers_cuit',
        'suppliers',
        ['cuit'],
        unique=True,
        postgresql_where=sa.text('cuit IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_suppliers_cuit', table_name='suppliers')
    op.drop_index('uq_suppliers_code', table_name='suppliers')
    op.drop_column('suppliers', 'status')
    op.drop_column('suppliers', 'iva_condition')
    op.drop_column('suppliers', 'code')
    op.drop_column('suppliers', 'whatsapp')
    op.drop_column('suppliers', 'email')
    op.drop_column('suppliers', 'address')
    op.drop_column('suppliers', 'cuit')
    op.alter_column('supplier_sku_mappings', 'confidence', new_column_name='confianza')
    op.alter_column('supplier_sku_mappings', 'internal_sku', new_column_name='sku_interno')
    op.alter_column('supplier_sku_mappings', 'raw_description', new_column_name='descripcion_raw')
    op.alter_column(
        'supplier_sku_mappings', 'supplier_sku_code', new_column_name='codigo_proveedor'
    )
    op.alter_column('supplier_sku_mappings', 'id', new_column_name='mapping_id')
    op.alter_column('supplier_sku_mappings', 'supplier_id', new_column_name='proveedor_id')
    op.rename_table('supplier_sku_mappings', 'proveedor_sku_mapping')
    op.alter_column('catalogo', 'supplier_id', new_column_name='proveedor_id')
    op.alter_column('suppliers', 'terms', new_column_name='condiciones')
    op.alter_column('suppliers', 'default_margin_pct', new_column_name='margen_predeterminado')
    op.alter_column('suppliers', 'phone', new_column_name='telefono')
    op.alter_column('suppliers', 'contact_name', new_column_name='contacto')
    op.alter_column('suppliers', 'business_name', new_column_name='razon_social')
    op.alter_column('suppliers', 'id', new_column_name='proveedor_id')
    op.rename_table('suppliers', 'proveedores')
    supplier_status.drop(op.get_bind(), checkfirst=True)
    iva_condition.drop(op.get_bind(), checkfirst=True)