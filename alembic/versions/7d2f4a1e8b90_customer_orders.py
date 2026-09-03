"""customer order pricing snapshots and exchange-rate settings

Revision ID: 7d2f4a1e8b90
Revises: 46bdbdc4a575
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7d2f4a1e8b90"
down_revision: str | None = "46bdbdc4a575"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable order totals, line snapshots, and manual pricing settings."""
    op.add_column("orders", sa.Column("subtotal", sa.Numeric(14, 2), nullable=True))
    op.add_column("orders", sa.Column("total", sa.Numeric(14, 2), nullable=True))
    op.add_column(
        "orders",
        sa.Column("conversion_pending", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("order_items", sa.Column("name", sa.String(300), nullable=True))
    op.add_column("order_items", sa.Column("source", sa.String(16), nullable=True))
    op.add_column("order_items", sa.Column("supplier", sa.String(120), nullable=True))
    op.add_column("order_items", sa.Column("moneda", sa.String(3), nullable=True))
    op.add_column("order_items", sa.Column("precio_original", sa.Numeric(14, 4), nullable=True))

    op.create_table(
        "exchange_rates",
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("rate_to_ars", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("currency"),
    )
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.execute("INSERT INTO exchange_rates (currency, rate_to_ars) VALUES ('ARS', 1.0000)")
    op.execute(
        sa.text(
            "INSERT INTO app_settings (key, value) VALUES (:key, :value)"
        ).bindparams(key="default_margin_pct", value="20")
    )


def downgrade() -> None:
    """Remove only the additive customer-order persistence objects."""
    op.drop_table("app_settings")
    op.drop_table("exchange_rates")
    op.drop_column("order_items", "precio_original")
    op.drop_column("order_items", "moneda")
    op.drop_column("order_items", "supplier")
    op.drop_column("order_items", "source")
    op.drop_column("order_items", "name")
    op.drop_column("orders", "conversion_pending")
    op.drop_column("orders", "total")
    op.drop_column("orders", "subtotal")
