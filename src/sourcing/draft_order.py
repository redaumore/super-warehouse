"""Persistence for source-aware orders assembled from chat draft lines."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from src.db.models import Cliente, Order, OrderEstado, OrderItem, SourcingState
from src.integrations.rag import normalize_rag_sku
from src.pricing.order_pricing import PricedLine, PricedOrder
from src.agents.inventory import reserve_stock


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
    delivery_date=None,
) -> Order:
    """Persist a priced draft and reserve stock only for LOCAL lines.

    RAG lines are immutable snapshots and deliberately do not require a catalog
    row or create a stock reservation. Sheets registration remains in the
    existing approval flow and is never called here.
    """
    order = Order(
        customer_id=customer.customer_id,
        estado=OrderEstado.PENDING_APPROVAL,
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
        sku = _stored_sku(line)
        if source == "LOCAL":
            reserve_stock(
                session,
                sku,
                customer.customer_id,
                line.cantidad,
                order_id=order.order_id,
            )
        session.add(
            OrderItem(
                order_id=order.order_id,
                sku=sku,
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
