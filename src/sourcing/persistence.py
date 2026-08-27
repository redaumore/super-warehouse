"""Case B persistence: SourcingNeed rows are the DB source of truth.

Per the order-sourcing spec, the missing items and the owner's supplier
selection must survive the in-memory 30-minute conversation TTL: they are
persisted on ``SourcingNeed`` rows keyed by the order. Re-selection before
execution simply updates ``supplier_id`` (the accumulation step then moves the
quantity between OPEN purchase orders).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import SourcingNeed


def upsert_sourcing_need(
    session: Session,
    order_id: int,
    sku: str,
    missing_quantity: int,
) -> SourcingNeed:
    """Create or update the order's need for one missing SKU."""
    need = session.scalar(
        select(SourcingNeed).where(
            SourcingNeed.order_id == order_id, SourcingNeed.sku == sku
        )
    )
    if need is None:
        need = SourcingNeed(order_id=order_id, sku=sku, missing_quantity=missing_quantity)
        session.add(need)
    else:
        need.missing_quantity = missing_quantity
    session.flush()
    return need


def record_supplier_selection(
    session: Session,
    need_id: int,
    supplier_id: int,
) -> SourcingNeed:
    """Persist the owner's supplier choice on a need (re-selection updates it)."""
    need = session.get(SourcingNeed, need_id)
    if need is None:
        raise KeyError(f"unknown sourcing need: {need_id}")
    need.supplier_id = supplier_id
    session.flush()
    return need


def sourcing_needs_for_order(session: Session, order_id: int) -> list[SourcingNeed]:
    """The order's needs ordered by SKU (stable for replies and accumulation)."""
    return list(
        session.scalars(
            select(SourcingNeed)
            .where(SourcingNeed.order_id == order_id)
            .order_by(SourcingNeed.sku)
        )
    )