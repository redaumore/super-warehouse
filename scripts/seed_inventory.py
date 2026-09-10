"""Idempotent inventory repair for catalog SKUs missing an Inventory row.

The historical catalog→Inventory backfill ended when the legacy
``catalogo.stock_disponible`` counter was retired (ADR 0002). The seed now
ensures every catalog product has an ``Inventory`` row, defaulting missing
ones to zero on hand (INSERT … ON CONFLICT (sku_id) DO NOTHING), so re-running
never overwrites live on-hand adjustments.

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
