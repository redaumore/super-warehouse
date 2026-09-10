"""Retire the legacy catalog stock counter (ADR 0002).

`Inventory.quantity_on_hand` is the single on-hand stock source. Every write
path already updates `Inventory` only, so the `catalogo.stock_disponible`
column is dead weight that can only drift: this migration drops it. The
downgrade re-adds the column as nullable with no backfill — historical values
are unrecoverable by design (the field was retired, not archived).

Revision ID: 934d98c5d1b2
Revises: d9fb1b9737e4
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "934d98c5d1b2"
down_revision: str | None = "d9fb1b9737e4"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.drop_column("catalogo", "stock_disponible")


def downgrade() -> None:
    op.add_column(
        "catalogo",
        sa.Column("stock_disponible", sa.Integer(), nullable=True),
    )
