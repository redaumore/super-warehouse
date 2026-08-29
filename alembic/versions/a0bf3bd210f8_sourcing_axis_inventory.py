"""sourcing axis + inventory

Adds the sourcing/fulfillment axis to orders (a separate column from the four
approval states), the canonical on-hand ``inventory`` table, and the
``supplier_purchase_order_state`` enum type used by the PO tables in the next
revision. The inventory backfill mirrors ``catalogo.stock_disponible`` into
``inventory.quantity_on_hand`` so the new availability source starts seeded.

Revision ID: a0bf3bd210f8
Revises: b2f353dfc3d2
Create Date: 2026-08-27 18:45:46.534646
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0bf3bd210f8"
down_revision: str | None = "b2f353dfc3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New enum types (additive; the four-state order_estado is untouched).
    sourcing_state = sa.Enum(
        "PENDING_ASSEMBLY", "IN_PREPARATION", "CANCELLED", name="sourcing_state"
    )
    sourcing_state.create(op.get_bind(), checkfirst=True)
    po_state = sa.Enum(
        "OPEN",
        "SENT",
        "PARTIALLY_RECEIVED",
        "FULLY_RECEIVED",
        "CANCELLED",
        name="supplier_purchase_order_state",
    )
    po_state.create(op.get_bind(), checkfirst=True)

    # Order sourcing axis: nullable-free with a server default so existing rows
    # backfill to PENDING_ASSEMBLY; delivery_date is informational and nullable.
    op.add_column(
        "orders",
        sa.Column(
            "sourcing_state",
            sa.Enum(
                "PENDING_ASSEMBLY",
                "IN_PREPARATION",
                "CANCELLED",
                name="sourcing_state",
            ),
            server_default="PENDING_ASSEMBLY",
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column("delivery_date", sa.Date(), nullable=True),
    )

    # Canonical on-hand stock table.
    op.create_table(
        "inventory",
        sa.Column("sku_id", sa.String(length=64), nullable=False),
        sa.Column("quantity_on_hand", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("sku_id"),
    )

    # Initial backfill: every catalog product's stock_disponible becomes the
    # starting quantity_on_hand.
    op.execute(
        "INSERT INTO inventory (sku_id, quantity_on_hand, updated_at) "
        "SELECT codigo_interno, stock_disponible, now() FROM catalogo"
    )


def downgrade() -> None:
    op.drop_table("inventory")
    op.drop_column("orders", "delivery_date")
    op.drop_column("orders", "sourcing_state")
    sa.Enum(name="supplier_purchase_order_state").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sourcing_state").drop(op.get_bind(), checkfirst=True)
