"""Case A: full-stock orders through the existing reservation + quotation flow.

Per the order-sourcing spec, a Case A order is created with sourcing
PENDING_ASSEMBLY and a delivery date, then routed through the unchanged
quotation/approval flow: every line is priced through the pure pricing engine,
soft-locked with the standard reservation TTL, and the owner receives the quote
in chat to approve (or reject). The separate Telegram push to ``owner_phone``
was removed — the quote travels as the agent's in-chat reply.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.inventory import reserve_stock
from src.agents.sales import ItemInput, Quote, quote_order
from src.db.models import Catalogo, Cliente, Order, OrderEstado, OrderItem, SourcingState
from src.orchestrator.session import ResolvedItem


class UnknownSkuError(Exception):
    """A resolved item has no catalog row to price from."""


def persist_case_a_order(
    session: Session,
    customer: Cliente,
    items: Sequence[ResolvedItem],
    *,
    delivery_date: date | None,
) -> tuple[Order, Quote]:
    """Persist a full-stock order: quote, order row, reservations, order items.

    Returns the created ``Order`` and its ``Quote``; the caller renders the
    quote as the in-chat reply so the unchanged approval flow resumes (awaiting
    the owner's decision).
    """
    inputs: list[ItemInput] = []
    for item in items:
        product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == item.sku))
        if product is None:
            raise UnknownSkuError(f"unknown sku: {item.sku}")
        inputs.append(
            ItemInput(
                sku=item.sku,
                cantidad=item.cantidad,
                base_price=product.precio_lista_base,
                description=product.nombre_oficial,
            )
        )
    quote = quote_order(
        inputs,
        customer.lista_precios.descuento_lista_pct,
        customer.descuento_particular_pct,
    )
    order = Order(
        customer_id=customer.customer_id,
        estado=OrderEstado.PENDING_APPROVAL,
        sourcing_state=SourcingState.PENDING_ASSEMBLY,
        delivery_date=delivery_date,
    )
    session.add(order)
    session.flush()
    for item in items:
        reserve_stock(
            session,
            item.sku,
            customer.customer_id,
            item.cantidad,
            order_id=order.order_id,
        )
    for item in items:
        line = quote.line_for(item.sku)
        session.add(
            OrderItem(
                order_id=order.order_id,
                sku=item.sku,
                cantidad=item.cantidad,
                base_price=line.base_price,
                final_price=line.final_price,
                adjustment=line.adjustment,
            )
        )
    session.flush()
    return order, quote
