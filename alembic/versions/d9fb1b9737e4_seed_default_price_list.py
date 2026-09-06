"""seed Default price list

Revision ID: d9fb1b9737e4
Revises: 0869e7146638
Create Date: 2026-09-06

Hand-written data migration (no autogenerate): inserts the guaranteed
"Default" price list (0% discount) required by the Settings → Listas de
precios CRUD and by `default_price_list_id`. The insert is idempotent — it
only runs when no row named "Default" exists — so re-running or pre-seeded
dev databases stay safe.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9fb1b9737e4"
down_revision: str | None = "0869e7146638"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Insert the Default list (0% discount) when it does not exist yet."""
    # Sync the serial first: explicit-id inserts (dev data, legacy seeds) do
    # not advance the sequence, so a blind nextval could collide with them.
    op.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('lista_precios', 'lista_id'),"
        "  COALESCE((SELECT MAX(lista_id) FROM lista_precios), 0) + 1,"
        "  false)"
    )
    op.execute(
        "INSERT INTO lista_precios (nombre, descuento_lista_pct) "
        "SELECT 'Default', 0 "
        "WHERE NOT EXISTS (SELECT 1 FROM lista_precios WHERE nombre = 'Default')"
    )


def downgrade() -> None:
    """Delete the seeded Default row, only while no client references it."""
    op.execute(
        "DELETE FROM lista_precios lp "
        "WHERE lp.nombre = 'Default' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM clientes c WHERE c.lista_precios_id = lp.lista_id)"
    )
