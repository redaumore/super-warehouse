"""align order_estado to the six-state order state machine

Revision ID: f2b2570aed04
Revises: 7d2f4a1e8b90
Create Date: 2026-09-04

Renames the four legacy approval states to their six-state-machine
equivalents and adds DRAFT + PICKING, then adds the one-draft-per-customer
partial unique index. `delivery_date` already exists as a nullable column
(added in a0bf3bd210f8) and is intentionally left untouched here.

AD1 (design): never drop an enum value — live rows land in the
diagram-equivalent state via RENAME VALUE, and the two extra labels survive
any downgrade because PostgreSQL cannot drop enum values without recreating
the type (documented, data-safe).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2b2570aed04"
down_revision: str | None = "7d2f4a1e8b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPE = "order_estado"


def _existing_labels(bind) -> set[str]:
    """Return the enum labels currently present on ``order_estado``."""
    rows = bind.execute(
        sa.text(
            "SELECT enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :type_name"
        ),
        {"type_name": _TYPE},
    ).scalars()
    return set(rows)


def upgrade() -> None:
    """Rename the four states, add DRAFT + PICKING, then index one-draft-per-customer."""
    bind = op.get_bind()
    labels = _existing_labels(bind)

    # RENAME VALUE is legal inside any transaction.
    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'PENDING_APPROVAL' TO 'CONFIRMED'")
    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'APPROVED' TO 'READY_FOR_DELIVERY'")
    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'IN_DISPATCH' TO 'CLOSED'")
    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'REJECTED' TO 'CANCELED'")

    # A downgrade leaves DRAFT/PICKING behind (PG cannot drop enum labels);
    # re-upgrading must not collide with those leftovers.
    pending = [value for value in ("DRAFT", "PICKING") if value not in labels]
    if pending:
        # Server version handling (design migration/rollout): PG < 12 rejects
        # ADD VALUE inside a transaction block; PG >= 12 accepts it but keeps
        # the new value unusable until commit — the partial index below needs
        # 'DRAFT' (in-transaction usage raises UnsafeNewEnumValueUsage). The
        # autocommit block covers both, so no version branch is needed.
        with op.get_context().autocommit_block():
            for value in pending:
                op.execute(f"ALTER TYPE {_TYPE} ADD VALUE '{value}'")

    # delivery_date already exists as a nullable column (a0bf3bd210f8); not re-added.
    op.create_index(
        "uq_orders_one_draft_per_customer",
        "orders",
        ["customer_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'DRAFT'"),
    )


def downgrade() -> None:
    """Reconcile new states back, reverse the renames, drop index; labels remain."""
    op.drop_index("uq_orders_one_draft_per_customer", table_name="orders")

    # Reconcile rows that landed in the new-only states back to the old
    # equivalents before renaming, so no row is stranded.
    op.execute("UPDATE orders SET estado = 'CONFIRMED' WHERE estado = 'DRAFT'")
    op.execute("UPDATE orders SET estado = 'READY_FOR_DELIVERY' WHERE estado = 'PICKING'")

    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'CONFIRMED' TO 'PENDING_APPROVAL'")
    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'READY_FOR_DELIVERY' TO 'APPROVED'")
    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'CLOSED' TO 'IN_DISPATCH'")
    op.execute(f"ALTER TYPE {_TYPE} RENAME VALUE 'CANCELED' TO 'REJECTED'")

    # delivery_date predates this migration (a0bf3bd210f8); not dropped here.
    # The DRAFT/PICKING enum labels remain: PostgreSQL cannot drop enum values.
