"""Persistence for source-aware orders assembled from chat draft lines.

The draft is persisted as an ``Order`` with ``estado=DRAFT`` (design AD2: Draft
is a persisted order row, never a memory-only buffer) with one ``OrderItem``
per line. The customer is resolved or created at the first add that knows it.

Reservations are deliberately NOT created here: per design AD10 the ACTIVE
soft-lock is created at the quote step (``cerrá el pedido``) and converted +
deducted at confirm — see ``src/agents/customer.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ExchangeRate,
    Order,
    OrderEstado,
    OrderItem,
    SourcingState,
)
from src.observability.session_logger import log_session_event
from src.pricing.order_pricing import (
    MissingRateError,
    PricedLine,
    PricedOrder,
    PricingLine,
    compute_order,
)
from src.sourcing.product_search import rag_products_table
from src.supplier.rag_catalog import normalize_rag_sku

_CENT = Decimal("0.01")

_LOCAL = "LOCAL"
_RAG = "RAG"


def _source_value(source: str | object) -> str:
    value = getattr(source, "value", source)
    return str(value).upper()


@dataclass(frozen=True)
class ManualLineInput:
    """One line typed/chosen in the manual order form, tagged with its source.

    ``source`` is ``"LOCAL"`` (local catalog + inventory) or ``"RAG"`` (indexed
    supplier catalog snapshot). The same SKU may legitimately appear as two
    lines — one LOCAL (covered by stock) and one RAG (the remainder) — because
    each source is an independent snapshot row on the order.
    """

    sku: str
    cantidad: int
    source: str = _LOCAL


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


def _normalize_manual_inputs(
    lines: Sequence[ManualLineInput | tuple[str, int]],
) -> tuple[ManualLineInput, ...]:
    """Validate and merge form lines into one input per (sku, source).

    Repeated lines for the SAME source accumulate quantity (chat-draft parity:
    the form grid already accumulates them, this is the defensive re-merge).
    Lines for the same SKU but DIFFERENT sources are intentionally kept apart —
    the owner may cover demand with local stock plus a RAG remainder.
    """
    merged: dict[tuple[str, str], list[int]] = {}
    order: list[tuple[str, str]] = []
    for raw in lines:
        if isinstance(raw, ManualLineInput):
            sku_text, quantity, source = raw.sku, raw.cantidad, raw.source
        else:
            if len(raw) != 2:
                raise ManualOrderError("every order line needs a SKU and a quantity")
            sku_text, quantity, source = raw[0], raw[1], _LOCAL
        sku_text = str(sku_text or "").strip()
        source_key = str(source or _LOCAL).strip().upper()
        if source_key not in (_LOCAL, _RAG):
            raise ManualOrderError(f"unsupported line source: {source_key}")
        if not sku_text:
            raise ManualOrderError("every order line needs a SKU")
        if int(quantity) <= 0:
            raise ManualOrderError(f"quantity must be positive for SKU {sku_text}")
        key = (source_key, sku_text)
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append(int(quantity))
    return tuple(
        ManualLineInput(sku=sku, cantidad=sum(merged[(source, sku)]), source=source)
        for source, sku in order
    )


def _compose_rag_name(marca: str | None, categoria: str | None, subcategoria: str | None) -> str:
    """Best-effort display name from RAG metadata (the table has no name column)."""
    return " ".join(part for part in (marca, categoria, subcategoria) if part).strip()


def _find_rag_product(session: Session, codigo: str) -> Any | None:
    """Exact lookup of one indexed RAG product by ``codigo_producto``."""
    table = rag_products_table(get_settings().rag_table_name)
    return session.execute(
        select(
            table.c.codigo_producto,
            table.c.codigo_proveedor,
            table.c.marca,
            table.c.categoria,
            table.c.subcategoria,
            table.c.precio,
            table.c.moneda,
        ).where(table.c.codigo_producto == codigo)
    ).first()


def _resolve_manual_pricing_lines(
    session: Session, inputs: Sequence[ManualLineInput]
) -> tuple[PricingLine, ...]:
    """Resolve manual inputs against their source catalogs as pricing lines.

    The manual path is catalog-only per line: LOCAL SKUs must exist in
    ``catalogo`` and RAG SKUs in ``catalogo_productos_rag`` — unknown SKUs and
    non-positive quantities are domain errors, never partial writes. RAG lines
    keep the raw ``codigo_producto`` as SKU; ``persist_draft_order`` normalizes
    the stored prefix (the search/form always speaks the raw catalog code).
    """
    pricing: list[PricingLine] = []
    for line in inputs:
        if line.source == _LOCAL:
            product = session.scalar(
                select(Catalogo).where(Catalogo.codigo_interno == line.sku)
            )
            if product is None:
                raise ManualOrderError(f"unknown SKU: {line.sku}")
            pricing.append(
                PricingLine(
                    sku=product.codigo_interno,
                    cantidad=int(line.cantidad),
                    source=_LOCAL,
                    name=product.nombre_oficial,
                    cost=product.costo_proveedor,
                    margin=product.margen_aplicado_pct,
                    currency="ARS",
                    supplier=product.supplier.code if product.supplier else None,
                )
            )
        else:
            row = _find_rag_product(session, line.sku)
            if row is None:
                raise ManualOrderError(f"unknown RAG product: {line.sku}")
            pricing.append(
                PricingLine(
                    sku=row.codigo_producto,
                    cantidad=int(line.cantidad),
                    source=_RAG,
                    name=_compose_rag_name(row.marca, row.categoria, row.subcategoria),
                    price=row.precio,
                    currency=row.moneda,
                    # The 3-char codigo_proveedor is the OrderItem.supplier
                    # contract (same convention as the chat draft lines).
                    supplier=row.codigo_proveedor,
                    codigo_proveedor=row.codigo_proveedor,
                )
            )
    return tuple(pricing)


def _rate_source(session: Session) -> Callable[[str], Decimal | None]:
    """Exchange-rate source backed by the current session (manual-path parity
    with the chat draft's resolver: same ``exchange_rates`` table)."""

    def rate(currency: str) -> Decimal | None:
        code = currency.strip().upper()
        if code == "ARS":
            return Decimal(1)
        return session.scalar(
            select(ExchangeRate.rate_to_ars).where(ExchangeRate.currency == code)
        )

    return rate


def _price_manual_lines(
    session: Session, customer: Cliente, pricing_lines: Sequence[PricingLine]
) -> PricedOrder:
    """Price the manual lines with the customer's list discount (no particular).

    Non-ARS lines convert through the manual ``exchange_rates`` table; a rate
    that is still missing (or any pending conversion) is refused as a friendly
    domain error — the manual path never persists half-priced drafts.
    """
    try:
        priced = compute_order(
            pricing_lines,
            rate=_rate_source(session),
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
    lines: Sequence[ManualLineInput | tuple[str, int]],
    *,
    delivery_date: date | None = None,
) -> Order:
    """Create a backoffice-initiated DRAFT order priced from the catalogs.

    Accepts plain ``(sku, cantidad)`` tuples (LOCAL, the original contract) or
    :class:`ManualLineInput` rows so the owner can mix LOCAL inventory lines
    and RAG snapshot lines in the same draft. The manual counterpart of the
    chat draft: same ``Order``/``OrderItem`` snapshot contract (via
    ``persist_draft_order``), same pricing engine, NO reservations and no
    Sheets (those belong to the confirm ceremony). The one-DRAFT-per-customer
    rule is enforced app-side first (AD4); the partial unique index remains
    the race backstop and its ``IntegrityError`` is translated into the same
    friendly domain error.
    """
    customer = session.get(Cliente, customer_id)
    if customer is None:
        raise ManualOrderError(f"unknown customer: {customer_id}")
    inputs = _normalize_manual_inputs(lines)
    if not inputs:
        raise ManualOrderError("at least one order line is required")
    draft = session.scalar(
        select(Order).where(Order.customer_id == customer_id, Order.estado == OrderEstado.DRAFT)
    )
    if draft is not None:
        raise ManualOrderError(
            f"customer {customer_id} already has an open draft order #{draft.order_id}"
        )
    priced = _price_manual_lines(session, customer, _resolve_manual_pricing_lines(session, inputs))
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
            "lines_count": len(inputs),
            "actor": "backoffice",
        },
    )
    return order


# ------------------------------------------------ manual draft modification


def _quantize_cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _item_key(sku: str, source: str | None) -> tuple[str, str]:
    """Identity of an order line: stored SKU + source (LOCAL is the default)."""
    return (sku, (source or _LOCAL).strip().upper())


def _recompute_draft_totals(session: Session, order: Order) -> None:
    """Refresh order.subtotal/total by re-summing the persisted line snapshots.

    Equivalent to a full ``compute_order`` re-price: pricing is per-line (list
    and particular discounts live inside each line's final price), so scaling
    or adding lines only shifts the sums, never any unit snapshot. Summed via
    SQL after flush — the in-session ``order.items`` collection does not
    include rows added/deleted in this same session until a refresh.
    """
    session.flush()
    subtotal, total = session.execute(
        select(
            func.coalesce(func.sum(OrderItem.base_price * OrderItem.cantidad), 0),
            func.coalesce(func.sum(OrderItem.final_price * OrderItem.cantidad), 0),
        ).where(OrderItem.order_id == order.order_id)
    ).one()
    order.subtotal = _quantize_cents(Decimal(subtotal))
    order.total = _quantize_cents(Decimal(total))


def update_manual_order(
    session: Session,
    order: Order,
    lines: Sequence[ManualLineInput | tuple[str, int]],
) -> Order:
    """Sync a DRAFT order's lines with the manual order form (priced, no zeros).

    SYNC semantics: the form was loaded with the draft's current lines, so the
    inputs describe the TARGET state of the draft. Accumulation rules:
    - an input matching an existing item's (stored SKU, source) keeps the
      item's frozen per-unit snapshot and moves only its quantity;
    - inputs absent from the draft delete those items (the owner removed the
      line in the form);
    - a NEW (sku, source) pair is resolved against its source catalog and
      priced right here via ``compute_order`` — a line is never persisted at 0;
    - the same SKU may hold one LOCAL and one RAG line at once (stock + RAG
      remainder); identical (sku, source) inputs are merged into one line.

    DRAFT-only: confirmed orders are out of scope (their reservations are
    already converted). Pending-conversion drafts are refused until the missing
    exchange rate exists — their non-ARS snapshots carry no ARS price, so the
    recomputed totals would silently undercount. New lines are priced BEFORE
    any mutation so a domain error (unknown SKU, missing rate) leaves the draft
    untouched.
    """
    if order.estado is not OrderEstado.DRAFT:
        raise ManualOrderError(
            f"order #{order.order_id} is not a draft ({order.estado.value}); "
            "only borradores can be modified"
        )
    if order.conversion_pending:
        raise ManualOrderError(
            f"order #{order.order_id} has a pending currency conversion; "
            "load the exchange rate in the backoffice first"
        )
    customer = order.customer
    if customer is None:
        raise ManualOrderError(f"order #{order.order_id} has no customer")
    inputs = _normalize_manual_inputs(lines)
    existing = {_item_key(item.sku, item.source): item for item in order.items}

    # Map every input to the identity it will have on the order. RAG inputs
    # speak the raw codigo_producto; the stored SKU collapses the doubled
    # provider prefix, so the row lookup supplies the provider code. A RAG row
    # that vanished from the catalog makes the whole sync fail (no partial
    # writes) — the owner re-searches and re-adds the line.
    resolved: list[tuple[tuple[str, str], ManualLineInput]] = []
    new_inputs: list[ManualLineInput] = []
    for line in inputs:
        if line.source == _RAG:
            row = _find_rag_product(session, line.sku)
            if row is None:
                raise ManualOrderError(f"unknown RAG product: {line.sku}")
            stored_sku = normalize_rag_sku(str(row.codigo_producto), str(row.codigo_proveedor or ""))
        else:
            stored_sku = line.sku
        key = _item_key(stored_sku, line.source)
        resolved.append((key, line))
        if key not in existing:
            new_inputs.append(line)

    # Resolve + price the NEW lines first: any failure (unknown SKU, missing
    # rate) aborts before a single row is touched (no partial writes).
    new_priced = (
        _price_manual_lines(session, customer, _resolve_manual_pricing_lines(session, new_inputs))
        if new_inputs
        else None
    )

    target_keys = {key for key, _ in resolved}
    for key, line in resolved:
        item = existing.get(key)
        if item is not None:
            item.cantidad = line.cantidad
    for key, item in existing.items():
        if key not in target_keys:
            session.delete(item)
    if new_priced is not None:
        for priced_line in new_priced.lines:
            session.add(
                OrderItem(
                    order_id=order.order_id,
                    sku=_stored_sku(priced_line),
                    cantidad=priced_line.cantidad,
                    base_price=priced_line.base_ars,
                    final_price=priced_line.final_ars,
                    adjustment=Decimal(0),
                    name=priced_line.name,
                    source=_source_value(priced_line.source),
                    supplier=priced_line.supplier,
                    moneda=priced_line.moneda,
                    precio_original=priced_line.precio_original,
                )
            )
    _recompute_draft_totals(session, order)
    session.flush()
    log_session_event(
        "orders",
        "update_manual_order",
        {
            "order_id": order.order_id,
            "lines_count": len(inputs),
            "total_ars": str(order.total),
            "actor": "backoffice",
        },
    )
    return order

