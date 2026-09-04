"""Case A: full-stock orders through the existing reservation + quotation flow.

Per the order-sourcing spec, a Case A order is created as a DRAFT at the first
add that knows the customer (design AD2): every line is priced through the pure
pricing engine, soft-locked with the standard reservation TTL at the quote step
(AD10 — the Draft stays DRAFT), and the owner receives the quote in chat to
confirm (or cancel). The confirm ceremony later classifies from the latest
availability and transitions DRAFT → CONFIRMED. The separate Telegram push to
``owner_phone`` was removed — the quote travels as the agent's in-chat reply.
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
from src.pricing.engine import compute_base


class UnknownSkuError(Exception):
    """A resolved item has no catalog row to price from."""


def persist_case_a_order(
    session: Session,
    customer: Cliente,
    items: Sequence[ResolvedItem],
    *,
    delivery_date: date | None,
) -> tuple[Order, Quote]:
    """Persist a full-stock order as DRAFT: quote, order row, reservations, items.

    Returns the created ``Order`` and its ``Quote``; the caller renders the
    quote as the in-chat reply so the confirm flow resumes (awaiting the
    owner's decision). The reservation is the ACTIVE soft-lock the confirm
    ceremony converts and deducts (AD10).
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
                # Source pricing: LOCAL base is costo_proveedor × (1 + margin),
                # never the hand-editable precio_lista_base (spec).
                base_price=compute_base(product.costo_proveedor, product.margen_aplicado_pct),
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
        estado=OrderEstado.DRAFT,
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
