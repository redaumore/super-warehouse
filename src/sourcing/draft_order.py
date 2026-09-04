"""Persistence for source-aware orders assembled from chat draft lines.

The draft is persisted as an ``Order`` with ``estado=DRAFT`` (design AD2: Draft
is a persisted order row, never a memory-only buffer) with one ``OrderItem``
per line. The customer is resolved or created at the first add that knows it.

Reservations are deliberately NOT created here: per design AD10 the ACTIVE
soft-lock is created at the quote step (``cerrá el pedido``) and converted +
deducted at confirm — see ``src/agents/customer.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.db.models import Cliente, Order, OrderEstado, OrderItem, SourcingState
from src.integrations.rag import normalize_rag_sku
from src.pricing.order_pricing import PricedLine, PricedOrder


def _source_value(source: str | object) -> str:
    value = getattr(source, "value", source)
    return str(value).upper()


def _stored_sku(line: PricedLine) -> str:
    """Normalize RAG SKU prefixes while leaving local catalog SKUs untouched."""
    if _source_value(line.source) != "RAG":
        return line.sku
    return normalize_rag_sku(line.sku, line.codigo_proveedor or "")


def persist_draft_order(
    session: Session,
    customer: Cliente,
    priced: PricedOrder,
    delivery_date: date | None = None,
) -> Order:
    """Persist a priced draft as a DRAFT order (no reservations, no Sheets).

    RAG lines are immutable snapshots and deliberately do not require a catalog
    row or create a stock reservation. Stock is soft-locked at the quote step
    (AD10) and Sheets registration runs on the confirm ceremony — never here.
    """
    order = Order(
        customer_id=customer.customer_id,
        estado=OrderEstado.DRAFT,
        sourcing_state=SourcingState.PENDING_ASSEMBLY,
        delivery_date=delivery_date,
        subtotal=priced.subtotal,
        total=priced.total,
        conversion_pending=priced.conversion_pending,
    )
    session.add(order)
    session.flush()

    for line in priced.lines:
        source = _source_value(line.source)
        session.add(
            OrderItem(
                order_id=order.order_id,
                sku=_stored_sku(line),
                cantidad=line.cantidad,
                base_price=line.base_ars,
                final_price=line.final_ars,
                adjustment=Decimal(0),
                name=line.name,
                source=source,
                supplier=line.supplier,
                moneda=line.moneda,
                precio_original=line.precio_original,
            )
        )
    session.flush()
    return order
