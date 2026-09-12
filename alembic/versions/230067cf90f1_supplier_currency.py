"""Supplier billing currency + catalog backfill.

`suppliers.moneda` records the currency a supplier bills its catalog in
(null = bills in ARS / not declared). Known per-supplier currencies are
backfilled in the same migration (SCO bills in USD), and every catalog row
adopted from a supplier that declares a currency but carries no `moneda` yet
inherits it — this repairs the SCO products that were adopted from receipts
before the column existed. The catalog backfill is generic (per-supplier
`moneda`), not hard-wired to SCO's id.

The downgrade drops the column; catalog currencies set by the backfill are
not reverted (they are legitimate data independent of the column's history).

Revision ID: 230067cf90f1
Revises: 934d98c5d1b2
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "230067cf90f1"
down_revision: str | None = "934d98c5d1b2"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("suppliers", sa.Column("moneda", sa.String(3), nullable=True))
    conn = op.get_bind()
    # Known billing currencies per supplier code (data migration, not schema).
    conn.execute(text("UPDATE suppliers SET moneda = 'USD' WHERE code = 'SCO'"))
    # Generic backfill: every catalog row without a currency inherits its
    # supplier's declared billing currency.
    conn.execute(
        text(
            "UPDATE catalogo SET moneda = s.moneda "
            "FROM suppliers s "
            "WHERE catalogo.supplier_id = s.id "
            "AND catalogo.moneda IS NULL "
            "AND s.moneda IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("suppliers", "moneda")
