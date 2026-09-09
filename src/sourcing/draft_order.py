"""Persistence for source-aware orders assembled from chat draft lines.

The draft is persisted as an ``Order`` with ``estado=DRAFT`` (design AD2: Draft
is a persisted order row, never a memory-only buffer) with one ``OrderItem``
per line. The customer is resolved or created at the first add that knows it.

Reservations are deliberately NOT created here: per design AD10 the ACTIVE
soft-lock is created at the quote step (``cerrá el pedido``) and converted +
deducted at confirm — see ``src/agents/customer.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import Catalogo, Cliente, Order, OrderEstado, OrderItem, SourcingState
from src.observability.session_logger import log_session_event
from src.pricing.order_pricing import (
    MissingRateError,
    PricedLine,
    PricedOrder,
    PricingLine,
    compute_order,
)
from src.supplier.rag_catalog import normalize_rag_sku


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
    log_session_event(
        "orders",
        "persist_draft_order",
        {
            "order_id": order.order_id,
            "customer_id": customer.customer_id,
            "lines_count": len(priced.lines),
            "total_ars": str(priced.total),
        },
    )
    return order


# ---------------------------------------------------- manual creation (backoffice)


class ManualOrderError(ValueError):
    """A backoffice-initiated order cannot be created as requested.

    One friendly domain error for every validation failure (unknown customer,
    empty lines, bad quantity, unknown SKU, an open draft already existing,
    unconvertible prices) so the UI surfaces a single actionable message.
    """


def _manual_pricing_lines(
    session: Session, lines: Sequence[tuple[str, int]]
) -> tuple[PricingLine, ...]:
    """Resolve (sku, cantidad) pairs against the local catalog as LOCAL lines.

    The manual path is catalog-only: every SKU must exist in ``catalogo`` and
    is priced from its snapshot cost + applied margin, mirroring the LOCAL
    branch of the chat draft pricing (``_draft_pricing_lines``). Unknown SKUs
    and non-positive quantities are domain errors, never partial writes.
    """
    pricing: list[PricingLine] = []
    for sku, cantidad in lines:
        sku_text = str(sku or "").strip()
        if not sku_text:
            raise ManualOrderError("every order line needs a SKU")
        if int(cantidad) <= 0:
            raise ManualOrderError(f"quantity must be positive for SKU {sku_text}")
        product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == sku_text))
        if product is None:
            raise ManualOrderError(f"unknown SKU: {sku_text}")
        pricing.append(
            PricingLine(
                sku=product.codigo_interno,
                cantidad=int(cantidad),
                source="LOCAL",
                name=product.nombre_oficial,
                cost=product.costo_proveedor,
                margin=product.margen_aplicado_pct,
                currency="ARS",
                supplier=product.supplier.code if product.supplier else None,
            )
        )
    return tuple(pricing)


def _price_manual_lines(
    session: Session, customer: Cliente, lines: Sequence[tuple[str, int]]
) -> PricedOrder:
    """Price the manual lines with the customer's list discount (no particular)."""
    try:
        priced = compute_order(
            _manual_pricing_lines(session, lines),
            list_discount=customer.lista_precios.descuento_lista_pct,
            # Particular discounts are intentionally out of scope (chat parity).
            particular_discount=Decimal(0),
        )
    except MissingRateError as exc:
        raise ManualOrderError(
            f"cannot price the order: {exc}. Load the exchange rate in the "
            "backoffice first."
        ) from exc
    if priced.conversion_pending:
        raise ManualOrderError(
            "cannot price the order: a price is pending currency conversion. "
            "Load the exchange rate in the backoffice first."
        )
    return priced


def create_manual_order(
    session: Session,
    customer_id: int,
    lines: Sequence[tuple[str, int]],
    *,
    delivery_date: date | None = None,
) -> Order:
    """Create a backoffice-initiated DRAFT order priced from the local catalog.

    The manual counterpart of the chat draft: same ``Order``/``OrderItem``
    snapshot contract (via ``persist_draft_order``), same pricing engine, NO
    reservations and no Sheets (those belong to the confirm ceremony). The
    one-DRAFT-per-customer rule is enforced app-side first (AD4); the partial
    unique index remains the race backstop and its ``IntegrityError`` is
    translated into the same friendly domain error.
    """
    customer = session.get(Cliente, customer_id)
    if customer is None:
        raise ManualOrderError(f"unknown customer: {customer_id}")
    if not lines:
        raise ManualOrderError("at least one order line is required")
    draft = session.scalar(
        select(Order).where(Order.customer_id == customer_id, Order.estado == OrderEstado.DRAFT)
    )
    if draft is not None:
        raise ManualOrderError(
            f"customer {customer_id} already has an open draft order #{draft.order_id}"
        )
    priced = _price_manual_lines(session, customer, lines)
    try:
        order = persist_draft_order(session, customer, priced, delivery_date)
    except IntegrityError:
        # Concurrent draft creation raced past the app-side guard: the DB
        # partial index rejected the second DRAFT. Roll back the partial write
        # and surface the same domain error the guard raises.
        session.rollback()
        raise ManualOrderError(
            f"customer {customer_id} already has an open draft order"
        ) from None
    log_session_event(
        "orders",
        "create_manual_order",
        {
            "order_id": order.order_id,
            "customer_id": customer.customer_id,
            "lines_count": len(lines),
            "actor": "backoffice",
        },
    )
    return order
