"""supplier purchase orders

Adds the supplier purchase order header + item tables (accumulating missing
items across customer orders) and the ``sourcing_needs`` link table that
persists each order's missing items and the owner's supplier selection.

Revision ID: 5f304e18a765
Revises: a0bf3bd210f8
Create Date: 2026-08-27 18:45:51.597129
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f304e18a765"
down_revision: str | None = "a0bf3bd210f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplier_purchase_orders",
        sa.Column("po_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column(
            "estado",
            # The enum type is already created by the sourcing-axis migration;
            # reference it without re-emitting CREATE TYPE.
            postgresql.ENUM(
                "OPEN",
                "SENT",
                "PARTIALLY_RECEIVED",
                "FULLY_RECEIVED",
                "CANCELLED",
                name="supplier_purchase_order_state",
                create_type=False,
            ),
            server_default="OPEN",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["proveedores.proveedor_id"],
        ),
        sa.PrimaryKeyConstraint("po_id"),
    )
    op.create_index(
        op.f("ix_supplier_purchase_orders_supplier_id"),
        "supplier_purchase_orders",
        ["supplier_id"],
        unique=False,
    )
    op.create_table(
        "supplier_purchase_order_items",
        sa.Column("po_item_id", sa.Integer(), nullable=False),
        sa.Column("po_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("received_quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["po_id"],
            ["supplier_purchase_orders.po_id"],
        ),
        sa.PrimaryKeyConstraint("po_item_id"),
        sa.UniqueConstraint("po_id", "sku", name="uq_supplier_po_item_sku"),
    )
    op.create_table(
        "sourcing_needs",
        sa.Column("need_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("missing_quantity", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("po_item_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.order_id"],
        ),
        sa.ForeignKeyConstraint(
            ["po_item_id"],
            ["supplier_purchase_order_items.po_item_id"],
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["proveedores.proveedor_id"],
        ),
        sa.PrimaryKeyConstraint("need_id"),
    )
    op.create_index(
        op.f("ix_sourcing_needs_order_id"),
        "sourcing_needs",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sourcing_needs_supplier_id"),
        "sourcing_needs",
        ["supplier_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_sourcing_needs_supplier_id"), table_name="sourcing_needs")
    op.drop_index(op.f("ix_sourcing_needs_order_id"), table_name="sourcing_needs")
    op.drop_table("sourcing_needs")
    op.drop_table("supplier_purchase_order_items")
    op.drop_index(
        op.f("ix_supplier_purchase_orders_supplier_id"),
        table_name="supplier_purchase_orders",
    )
    op.drop_table("supplier_purchase_orders")
