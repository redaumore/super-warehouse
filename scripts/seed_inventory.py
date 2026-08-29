"""Idempotent inventory backfill from the legacy catalog stock counter.

Mirrors every catalog product's ``stock_disponible`` into the canonical
``Inventory.quantity_on_hand`` (INSERT … ON CONFLICT (sku_id) DO NOTHING), so
re-running never overwrites live on-hand adjustments. The Alembic migration
already backfills once at upgrade time; this script is for existing databases
and for re-seeding after a manual reset.

Usage:
    python3 scripts/seed_inventory.py
"""

from __future__ import annotations

from src.agents.inventory import seed_inventory
from src.db.session import SessionLocal


def main() -> None:
    with SessionLocal() as session:
        inserted = seed_inventory(session)
        session.commit()
    print(f"Inventory seeded: {inserted} row(s) inserted (existing rows untouched).")


if __name__ == "__main__":
    main()
