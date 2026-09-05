"""Customer agent: owner-assistant catalog context and conversational replies.

The owner is the only chat actor: the persona is the owner's assistant, not a
customer-facing chatbot. Every text turn is answered by the LLM responder over
the full context (system prompt + prior history + the new message). Slice 2
adds a search boundary: when a ``ProductSearcher`` is wired, the turn's text is
resolved through the local-first → RAG-fallback precedence chain
(``PrecedenceProductSearcher``) and the source-discriminated result becomes a
TRANSIENT system note injected right before the user turn — never persisted
into history.

The note is source-aware (ADR 5): LOCAL hits list ``nombre_oficial (sku)``
under own stock; RAG hits are numbered, cheapest first, with provider, price,
specs and source page/PDF, plus a footer clarifying they are supplier-catalog
items, not own stock; NONE means "not found in current catalogs" with a
reformulation suggestion; ERROR means the supplier catalogs could not be
consulted. It never claims the item is out of stock either way. A SQLAlchemy
error from the searcher skips the note instead of failing the turn, so the
conversation keeps answering while the DB is down.

Order building rides the same seam: while an order is open, the owner can add
the last displayed product with natural phrases ("agregalo", "sumá 5 de eso",
"el 2", or a bare quantity answer like "quiero 2") and the handler appends it
to the state's ``draft_items`` without calling the LLM; the add reply invites
the owner to finalize with "cerrá el pedido para <cliente>". The guided
(scripted) flow is the ONLY order-creation path: the legacy free-form parsed
intake was removed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, TypedDict

import phonenumbers
from phonenumbers import PhoneNumber, PhoneNumberFormat
from phonenumbers.phonenumberutil import NumberParseException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from src.agents.commands import (
    ADD_QUANTITY,
    APPROVE,
    FINALIZE_WITH_CUSTOMER,
    NEW_CUSTOMER,
    REJECT,
)
from src.agents.customers import (
    CustomerResolutionKind,
    format_customer_menu,
    parse_create_client_command,
    parse_customer_pick,
    resolve_customer_name,
)
from src.agents.disambiguation import (
    SearchCandidate,
    normalize_text,
    search_catalog,
)
from src.agents.product_search import (
    ProductEntry,
    ProductSearcher,
    ProductSearchResult,
    ProductSource,
    is_finalize,
    parse_finalize,
    parse_product_add,
    parse_product_remove,
)
from src.channels.base import InboundMessage
from src.db.models import AppSetting, Catalogo, Cliente, ExchangeRate, Order, OrderEstado, Supplier
from src.db.session import SessionLocal
from src.integrations.rag import RagProductClient
from src.orchestrator.router import AgentOutcome, RoutingDecision
from src.orchestrator.session import ChatMessage, ConversationState, ResolvedItem
from src.order_lifecycle.state import remove_draft_item
from src.pricing.order_pricing import (
    MarginSource,
    MissingRateError,
    PricedOrder,
    PricingLine,
    RateSource,
    compute_order,
    line_subtotal,
    pending_order,
)
from src.sourcing.classify import MissingItem
from src.sourcing.draft_order import persist_draft_order
from src.supplier.searcher import SupplierCatalogSearcher

logger = logging.getLogger(__name__)

_DEFAULT_REGION = "AR"

# Fallback reply when the LLM responder is unavailable: the guided flow is the
# only order path, so the fallback points at its session-reset trigger.
GREETING = "¿Qué pedido cargamos hoy? Mandá 'hola bob' y lo armamos paso a paso."

# Add-intent short-circuit replies (owner-facing, rioplatense).
ADDED_TO_ORDER_REPLY = (
    "Listo: agregué {name} × {qty} al pedido en curso{price}. "
    "¿Algo más? Si ya está completo, decime 'cerrá el pedido para <cliente>'."
)

EMPTY_DRAFT_FINALIZE_REPLY = (
    "Todavía no hay productos en el pedido. Buscá el artículo (por ejemplo "
    "'recolector de aceite') y después sumalo con 'agregale 2'. Cuando esté "
    "completo, decime 'cerrá el pedido para <cliente>'."
)

ASK_CUSTOMER_FINALIZE_REPLY = (
    "¿Para qué cliente es el pedido? Decime 'cerrá el pedido para <cliente>'. "
    "Si es nuevo, dalo de alta con 'nuevo cliente <nombre> <teléfono>'."
)

SYSTEM_PROMPT = (
    "Sos el asistente del dueño de una ferretería de barrio, y te escribe el dueño "
    "por WhatsApp o Telegram. Tu trabajo es cargar los pedidos de sus clientes: "
    "primero identificás al cliente por nombre, después los productos y cantidades. "
    "Respondé en español rioplatense, con tono cálido y directo, en mensajes cortos. "
    "Los pedidos los crea la app, no el chat: nunca digas que un pedido quedó guardado, "
    "confirmado, creado, cerrado o cancelado, que le agregaste productos, ni que lo "
    "estás gestionando, ni prometas avisar cuando esté listo. "
    "Nunca inventes cantidades que el dueño no escribió. "
    "Cuando mostrás un producto, respondé breve y no cierres la venta: la app se encarga "
    f"de agregar y finalizar (el dueño puede escribir '{ADD_QUANTITY}', "
    f"'{FINALIZE_WITH_CUSTOMER}' o '{NEW_CUSTOMER}'). "
    "Cuando el dueño te pida confirmar, cerrar o finalizar un pedido, no lo hagas vos: "
    "recordale el comando exacto para hacerlo."
)


def format_added_to_order_reply(entry: ProductEntry, qty: int) -> str:
    """Render the add-to-order confirmation, with a price part when known.

    The price renders exactly like ``_rag_entry_fields``: ``{price:g}`` followed
    by currency and/or unit only when present. An entry without a price renders
    no price part at all.
    """
    price_part = ""
    if entry.price is not None:
        price_text = f"{entry.price:g}"
        if entry.currency and entry.unit:
            price_text = f"{price_text} {entry.currency}/{entry.unit}"
        elif entry.currency:
            price_text = f"{price_text} {entry.currency}"
        elif entry.unit:
            price_text = f"{price_text}/{entry.unit}"
        price_part = f" ({price_text})"
    return ADDED_TO_ORDER_REPLY.format(name=entry.name, qty=qty, price=price_part)


def _to_whatsapp_e164(number: PhoneNumber) -> str:
    """Render an Argentine number in WhatsApp mobile form (+54 9 …).

    WhatsApp customers always reach the store from a mobile line, so a national
    number without the trunk prefix ``9`` (e.g. ``11 5555 1234``) is completed
    to ``+54 9 11 5555 1234``. This keeps every variant of the same number
    converging on one canonical form. Landline rendering is out of MVP scope.
    """
    e164 = phonenumbers.format_number(number, PhoneNumberFormat.E164)
    if number.country_code == 54 and not str(number.national_number).startswith("9"):
        return f"+549{number.national_number}"
    return e164


def normalize_phone(raw: str, *, region: str = _DEFAULT_REGION) -> str | None:
    """Normalize a phone string to canonical E.164; ``None`` when unparseable.

    ``region`` is the default region for numbers without an explicit country
    code (the store's home country).
    """
    try:
        number = phonenumbers.parse(raw, region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return _to_whatsapp_e164(number)


class ResponderError(Exception):
    """Base error for the conversational responder."""


class ResponderNotConfigured(ResponderError):
    """Raised when the responder has no API key (caller may fall back)."""


class CustomerResponder(Protocol):
    """Mockable LLM boundary the Customer agent talks through (real impl: OpenAIResponder)."""

    def respond(self, messages: Sequence[ChatMessage]) -> str:
        """Answer the customer from the full message list (system + history + latest user turn)."""
        ...


class DbCatalogSearcher:
    """Local catalog search backed by Postgres; one short-lived session per call.

    The local hop of the ``PrecedenceProductSearcher`` chain (structurally
    satisfies ``LocalSearcher``). The with-block closes the session even when
    the query raises, so the Customer agent never leaks connections across
    turns.
    """

    def search(self, query: str) -> tuple[SearchCandidate, ...]:
        """Open a session, run the hybrid catalog search, return candidates."""
        with SessionLocal() as session:
            return tuple(search_catalog(session, query, limit=3))


def _cheapest_first(entries: Sequence[ProductEntry]) -> list[ProductEntry]:
    """Sort product entries by ascending price; entries without price go last."""
    return sorted(
        entries,
        key=lambda e: (e.price is None, e.price if e.price is not None else 0.0),
    )


def _rag_entry_fields(entry: ProductEntry) -> str:
    """Render one RAG entry: name, brand, provider, price, specs, source page/PDF."""
    parts = [entry.name]
    if entry.brand:
        parts.append(entry.brand)
    if entry.provider:
        parts.append(entry.provider)
    if entry.price is not None:
        price_text = f"{entry.price:g}"
        if entry.currency and entry.unit:
            price_text = f"{price_text} {entry.currency}/{entry.unit}"
        elif entry.currency:
            price_text = f"{price_text} {entry.currency}"
        elif entry.unit:
            price_text = f"{price_text}/{entry.unit}"
        parts.append(price_text)
    if entry.specs:
        parts.append(entry.specs)
    if entry.source_file:
        source_text = entry.source_file
        if entry.page is not None:
            source_text = f"{source_text} p.{entry.page}"
        parts.append(source_text)
    return " — ".join(parts)


def _local_note(query: str, entries: Sequence[ProductEntry]) -> str:
    """Render a LOCAL-only note: numbered official names with SKUs."""
    lines = [f"Catalog results for «{query}» — own stock:"]
    lines.extend(f"{i}. {entry.name} ({entry.sku})" for i, entry in enumerate(entries, 1))
    return "\n".join(lines)


def _rag_note(query: str, entries: Sequence[ProductEntry]) -> str:
    """Render a RAG-only note: numbered, cheapest first, with the supplier footer."""
    lines = [f"Catalog results for «{query}» — supplier catalog:"]
    lines.extend(
        f"{i}. {_rag_entry_fields(entry)}" for i, entry in enumerate(_cheapest_first(entries), 1)
    )
    lines.append("These are supplier-catalog items, not own stock.")
    return "\n".join(lines)


def _dual_note(query: str, local: Sequence[ProductEntry], rag: Sequence[ProductEntry]) -> str:
    """Render a mixed local + RAG note: local block first, global numbering, labeled."""
    lines = [f"Catalog results for «{query}» — own stock (local):"]
    number = 1
    for entry in local:
        lines.append(f"{number}. {entry.name} ({entry.sku}) [local]")
        number += 1
    lines.append(f"Catalog results for «{query}» — supplier catalog (rag):")
    for entry in _cheapest_first(rag):
        lines.append(f"{number}. {_rag_entry_fields(entry)} [rag]")
        number += 1
    return "\n".join(lines)


def product_context_note(
    query: str,
    result: ProductSearchResult,
    *,
    draft: tuple[ProductEntry, ...] = (),
) -> str:
    """Render product-query results as a transient system note (English copy, ADR 5).

    The note is internal guidance for the owner's assistant. RAG results are
    numbered and sorted cheapest first; a draft accumulation that mixes local
    and RAG entries (accumulated across queries during order building) renders
    local-first with each block labeled by source. NONE/ERROR notes never claim
    the item is out of stock.
    """
    if result.source is ProductSource.NONE:
        return (
            f"Catalog results for «{query}»: no match in current catalogs. "
            "Ask the owner for a synonym or reformulation. "
            "Do not claim the item is out of stock."
        )
    if result.source is ProductSource.ERROR:
        return (
            f"Catalog results for «{query}»: supplier catalogs could not be "
            "consulted. Tell the owner they are unavailable and offer to retry "
            "later. Do not claim the item is out of stock."
        )
    draft_local = tuple(e for e in draft if e.source is ProductSource.LOCAL)
    draft_rag = tuple(e for e in draft if e.source is ProductSource.RAG)
    if result.source is ProductSource.RAG and draft_local:
        return _dual_note(query, draft_local, result.entries)
    if result.source is ProductSource.LOCAL and draft_rag:
        return _dual_note(query, result.entries, draft_rag)
    if result.source is ProductSource.RAG:
        return _rag_note(query, result.entries)
    return _local_note(query, result.entries)


@dataclass
class SourcingDeps:
    """Boundaries the sourcing turn needs (session + supplier searcher).

    Quotes, cancellations and approvals are in-chat replies, so no notifier or
    owner phone is bridged anymore — the pipeline edge owns the only outbound.
    """

    session_factory: Callable[[], Session]
    searcher: SupplierCatalogSearcher
    rag_client: RagProductClient | None = None


def format_case_b_reply(order: Order, missing: tuple[MissingItem, ...]) -> str:
    """Case B reply: list each missing item and its numbered supplier options."""
    lines = [
        (
            f"Pedido #{order.order_id}: hay artículos sin stock. "
            "Elegí supplier para cada uno (respondé los números):"
        )
    ]
    number = 1
    for item in missing:
        options = "  ".join(
            f"{number + i}) {candidate.business_name}"
            + (
                f" ({candidate.available_quantity})"
                if candidate.available_quantity is not None
                else ""
            )
            for i, candidate in enumerate(item.candidates)
        )
        name = item.description or item.sku
        lines.append(f"- {name}: faltan {item.missing_quantity}. {options}")
        number += len(item.candidates)
    return "\n".join(lines)


def format_case_c_reply(
    order: Order, missing: tuple[MissingItem, ...], customer_name: str | None = None
) -> str:
    """Case C reply: tell the owner the missing items are unavailable."""
    who = f" de {customer_name}" if customer_name else ""
    names = ", ".join(item.description or item.sku for item in missing)
    return f"Pedido #{order.order_id}{who} cancelado: {names} no están disponibles por el momento."


def unmapped_supplier_note(searcher: object | None) -> str:
    """Owner-facing note for a Case C caused by RAG hits with unmapped providers.

    Reads the searcher's ``last_unmapped_codes`` diagnostic (duck-typed so any
    searcher exposing it works). Returns an empty string when the searcher does
    not report dropped codes — then the generic unavailable reply stands alone.
    The codes ARE the actionable part: the supplier master is missing entries
    the ingesta should have matched (auto-creating suppliers is not allowed).
    """
    codes = tuple(dict.fromkeys(getattr(searcher, "last_unmapped_codes", ()) or ()))
    if not codes:
        return ""
    return (
        "\n\nAtención: el catálogo de proveedores tiene productos cuyo proveedor "
        f"(códigos: {', '.join(codes)}) no está cargado en la maestra. "
        "Revisá la ingesta de listas antes de reintentar."
    )


def _handle_create_client(
    deps: SourcingDeps,
    base: ConversationState,
    nombre: str,
    telefono: str,
) -> AgentOutcome:
    """Create a client in chat: ``nuevo cliente <nombre> <teléfono>``.

    Reuses ``backoffice.clients.create_client`` with the default (Base) price
    list. A phone that already belongs to a client reports the existing client
    instead of creating a duplicate (locked input #2); any other invalid input
    is reported as an error.
    """
    from src.backoffice.clients import (
        InvalidClientDataError,
        create_client,
        default_price_list_id,
    )

    with deps.session_factory() as session:
        normalized = normalize_phone(telefono)
        existing = (
            session.scalar(select(Cliente).where(Cliente.telefono_norm == normalized))
            if normalized is not None
            else None
        )
        if existing is not None:
            return AgentOutcome(
                state=base,
                reply=(
                    f"Ese teléfono ya es de {existing.nombre_comercial}; "
                    "no creé un duplicado. Usalo por nombre en el próximo pedido."
                ),
            )
        try:
            client = create_client(
                session,
                nombre_comercial=nombre,
                telefono_raw=telefono,
                lista_precios_id=default_price_list_id(session),
            )
        except InvalidClientDataError as exc:
            session.rollback()
            return AgentOutcome(
                state=base,
                reply=f"No pude crear el cliente: {exc}",
            )
        session.commit()
    reply = f"Listo: di de alta a {client.nombre_comercial}. Ahora mandá el pedido con su nombre."
    return AgentOutcome(state=base, reply=reply)


class DraftPricingError(ValueError):
    """The draft cannot be priced from the available product snapshot."""


def _default_margin(session: Session) -> Decimal:
    """Read the DB default margin and normalize points or fractions."""
    setting = session.get(AppSetting, "default_margin_pct")
    if setting is None:
        return Decimal("0.20")
    value = Decimal(setting.value)
    return value / Decimal(100) if value.copy_abs() > 1 else value


def _rate_source(session: Session) -> Callable[[str], Decimal | None]:
    """Build an exchange-rate source backed by the current session."""

    def rate(currency: str) -> Decimal | None:
        code = currency.strip().upper()
        if code == "ARS":
            return Decimal(1)
        return session.scalar(select(ExchangeRate.rate_to_ars).where(ExchangeRate.currency == code))

    return rate


def _supplier_margin_source(session: Session) -> Callable[[str | None], Decimal | None]:
    """Resolve a RAG supplier code to its current default margin."""

    def margin(code: str | None) -> Decimal | None:
        if not code:
            return None
        return session.scalar(
            select(Supplier.default_margin_pct).where(Supplier.code == code.strip().upper())
        )

    return margin


def _rag_price(
    entry: ProductEntry, rag_client: RagProductClient | None
) -> tuple[float | Decimal, str | None]:
    """Use the displayed RAG price, falling back to the sibling service."""
    if entry.price is not None:
        return entry.price, entry.currency
    if rag_client is None:
        raise DraftPricingError(f"RAG price unavailable for SKU {entry.sku}")
    lookup = rag_client.price_lookup(entry.sku, entry.codigo_proveedor or entry.provider)
    if lookup is None or lookup.price is None:
        raise DraftPricingError(f"RAG price unavailable for SKU {entry.sku}")
    return lookup.price, lookup.currency


def _draft_pricing_lines(
    session: Session,
    base: ConversationState,
    rag_client: RagProductClient | None,
) -> tuple[PricingLine, ...]:
    """Enrich draft entries with local cost or RAG fallback price data."""
    lines: list[PricingLine] = []
    for entry, quantity in base.draft_items:
        source = getattr(entry.source, "value", entry.source)
        if str(source).upper() == ProductSource.LOCAL.value:
            product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == entry.sku))
            if product is None:
                raise DraftPricingError(f"local product not found for SKU {entry.sku}")
            lines.append(
                PricingLine(
                    sku=entry.sku,
                    cantidad=quantity,
                    source=entry.source,
                    name=entry.name,
                    cost=product.costo_proveedor,
                    margin=product.margen_aplicado_pct,
                    currency="ARS",
                    supplier=product.supplier.code if product.supplier else None,
                )
            )
            continue
        if str(source).upper() != ProductSource.RAG.value:
            raise DraftPricingError(f"unsupported product source for SKU {entry.sku}")
        price, currency = _rag_price(entry, rag_client)
        code = entry.codigo_proveedor or entry.provider
        lines.append(
            PricingLine(
                sku=entry.sku,
                cantidad=quantity,
                source=entry.source,
                name=entry.name,
                price=price,
                currency=currency,
                # Prefer the 3-char codigo_proveedor: the backoffice treats
                # OrderItem.supplier AS the codigo_proveedor code, and the
                # confirm ceremony resolves it against the supplier master.
                # The provider display name is only the no-code fallback.
                supplier=code or entry.provider,
                codigo_proveedor=code,
            )
        )
    return tuple(lines)


class DraftPricingSources(TypedDict):
    """Typed pricing keyword bundle shared by ``compute_order`` and ``pending_order``."""

    rate: RateSource
    supplier_margin: MarginSource
    default_margin: Decimal
    list_discount: Decimal
    particular_discount: Decimal


def _price_draft(
    session: Session,
    customer: Cliente,
    base: ConversationState,
    rag_client: RagProductClient | None,
) -> PricedOrder:
    """Price a draft or produce its pending-conversion snapshot."""
    lines = _draft_pricing_lines(session, base, rag_client)
    kwargs: DraftPricingSources = {
        "rate": _rate_source(session),
        "supplier_margin": _supplier_margin_source(session),
        "default_margin": _default_margin(session),
        "list_discount": customer.lista_precios.descuento_lista_pct,
        # Particular discounts are intentionally out of this change's scope.
        "particular_discount": Decimal(0),
    }
    try:
        return compute_order(lines, **kwargs)
    except MissingRateError:
        return pending_order(lines, **kwargs)


def _draft_quote_reply(order: Order, customer: Cliente, priced: PricedOrder) -> str:
    """Render the owner-facing quote for a newly persisted draft.

    Multi-line layout (one item per line with its quantity and subtotal, the
    total at the end) so a multi-item order stays readable in chat.
    """
    if priced.conversion_pending:
        return (
            f"Pedido #{order.order_id} para {customer.nombre_comercial}: conversión pendiente. "
            "Cargá el tipo de cambio que falta en Customer Orders y aprobalo."
        )
    lines = [
        f"{line.cantidad} × {line.name or line.sku} — "
        f"{line_subtotal(line.final_ars, line.cantidad):.2f} ARS"
        for line in priced.lines
    ]
    return (
        f"Pedido #{order.order_id} para {customer.nombre_comercial}:\n"
        + "\n".join(lines)
        + f"\nTotal: {priced.total:.2f} ARS\n"
        + f"Respondé '{APPROVE}' o '{REJECT}'."
    )


def _existing_draft(session: Session, customer_id: int) -> Order | None:
    """The customer's open DRAFT order, if any (single-draft app guard)."""
    return session.scalar(
        select(Order).where(Order.customer_id == customer_id, Order.estado == OrderEstado.DRAFT)
    )


def _reserve_quote_lines(
    session: Session, customer: Cliente, order: Order, priced: PricedOrder
) -> None:
    """AD10: the quote step soft-locks LOCAL lines with an ACTIVE reservation.

    RAG lines are supplier snapshots and never reserve stock; the Draft stays
    DRAFT. The confirm ceremony converts these reservations and deducts stock.
    A LOCAL line whose quantity exceeds the available stock is left
    UNRESERVED (the soft-lock is all-or-nothing per line): the draft still
    persists and the confirm ceremony classifies the gap from the latest
    availability — Case B (supplier selection) or Case C (cancel) — instead
    of crashing the finalize turn.
    """
    from src.agents.inventory import InsufficientStockError, reserve_stock

    for line in priced.lines:
        source = getattr(line.source, "value", line.source)
        if str(source).upper() != "LOCAL":
            continue
        try:
            reserve_stock(
                session,
                line.sku,
                customer.customer_id,
                line.cantidad,
                order_id=order.order_id,
            )
        except InsufficientStockError:
            # Stock gap: no partial soft-lock. The confirm ceremony re-classifies.
            continue


def persist_finalized_draft(
    session: Session,
    customer: Cliente,
    base: ConversationState,
    rag_client: RagProductClient | None,
) -> AgentOutcome:
    """Price, persist, and reserve a draft for a resolved customer (quote step).

    The one persistence path shared by the free-form finalize command and the
    guided (scripted) order-creation flow. The first add that knows the
    customer persists an ``Order`` with ``estado=DRAFT`` (design AD2). Per
    AD10 the quote step soft-locks the LOCAL lines with an ACTIVE reservation
    while the Draft stays DRAFT; RAG lines never reserve. The single-draft
    rule (spec: at most one DRAFT per customer) is enforced by an app guard
    plus the ``uq_orders_one_draft_per_customer`` partial index as the DB
    backstop for the add race.
    """
    try:
        priced = _price_draft(session, customer, base, rag_client)
    except DraftPricingError as exc:
        return AgentOutcome(state=base, reply=f"I could not price the draft: {exc}")
    existing = _existing_draft(session, customer.customer_id)
    if existing is not None:
        return AgentOutcome(
            state=base,
            reply=(
                f"{customer.nombre_comercial} ya tiene un pedido abierto "
                f"(pedido #{existing.order_id}); no creé otro. "
                "Continuá con ese pedido y después confirmalo."
            ),
        )
    try:
        order = persist_draft_order(session, customer, priced)
        _reserve_quote_lines(session, customer, order, priced)
    except IntegrityError:
        # The single-draft race: another session persisted the DRAFT first.
        session.rollback()
        return AgentOutcome(
            state=base,
            reply=(
                f"{customer.nombre_comercial} ya tiene un pedido abierto; "
                "no creé otro. Continuá con el pedido existente."
            ),
        )
    session.commit()
    updated = base.with_updates(
        customer_id=customer.customer_id,
        order_id=order.order_id,
        items=tuple(
            ResolvedItem(sku=line.sku, cantidad=line.cantidad, description=line.name)
            for line in priced.lines
        ),
        awaiting_decision=True,
        customer_disambiguation_pending=False,
        customer_candidates=(),
        product_options=(),
        draft_items=(),
    )
    return AgentOutcome(state=updated, reply=_draft_quote_reply(order, customer, priced))


def _create_customer_for_draft(
    session: Session,
    base: ConversationState,
    nombre: str,
    telefono: str,
    rag_client: RagProductClient | None,
) -> AgentOutcome:
    """Create or reuse a client, then attach the waiting draft immediately."""
    from src.backoffice.clients import InvalidClientDataError, create_client, default_price_list_id

    normalized = normalize_phone(telefono)
    if normalized is None:
        return AgentOutcome(
            state=base, reply=f"I could not create the customer: invalid phone {telefono}"
        )
    customer = session.scalar(select(Cliente).where(Cliente.telefono_norm == normalized))
    if customer is None:
        try:
            customer = create_client(
                session,
                nombre_comercial=nombre,
                telefono_raw=telefono,
                lista_precios_id=default_price_list_id(session),
            )
        except InvalidClientDataError as exc:
            session.rollback()
            return AgentOutcome(state=base, reply=f"I could not create the customer: {exc}")
    return persist_finalized_draft(session, customer, base, rag_client)


def _remove_target_matches(needle: str, name: str | None, sku: str) -> bool:
    """True when a normalized remove target names the line (name or SKU)."""
    return (name is not None and needle in normalize_text(name)) or needle in normalize_text(sku)


def _resolve_persisted_line(session: Session, order: Order, needle: str) -> str | None:
    """Resolve a remove target against the persisted OrderItem rows."""
    from src.db.models import OrderItem

    items = session.scalars(select(OrderItem).where(OrderItem.order_id == order.order_id)).all()
    for item in items:
        if _remove_target_matches(needle, item.name, item.sku):
            return item.sku
    return None


def _run_remove_product_turn(
    message: InboundMessage,
    base: ConversationState,
    deps: SourcingDeps | None,
) -> AgentOutcome | None:
    """Handle the remove-product command; ``None`` when the text is not one.

    Removes the referenced line from the in-memory draft (``draft_items``) or —
    when the conversation resumed a persisted DRAFT order — from its
    ``OrderItem`` rows via ``remove_draft_item`` (an empty Draft stays DRAFT,
    spec: remove product is real and the draft persists).
    """
    target = parse_product_remove(message.text or "")
    if target is None:
        return None
    needle = normalize_text(target)
    kept: list[tuple[ProductEntry, int]] = []
    removed: list[str] = []
    for entry, qty in base.draft_items:
        if _remove_target_matches(needle, entry.name, entry.sku):
            removed.append(entry.name or entry.sku)
        else:
            kept.append((entry, qty))
    if removed:
        return AgentOutcome(
            state=base.with_updates(draft_items=tuple(kept)),
            reply=f"Listo: saqué {' y '.join(removed)} del pedido en curso.",
        )
    if base.order_id is not None and deps is not None:
        with deps.session_factory() as session:
            order = session.get(Order, base.order_id)
            if order is not None and order.estado is OrderEstado.DRAFT:
                sku = _resolve_persisted_line(session, order, needle)
                if sku is not None:
                    remove_draft_item(session, order, sku)
                    session.commit()
                    return AgentOutcome(
                        state=base,
                        reply=f"Listo: saqué {sku} del pedido #{order.order_id}.",
                    )
    return AgentOutcome(
        state=base,
        reply="No encontré ese artículo en el pedido en curso.",
    )


def _run_finalize_turn(
    message: InboundMessage,
    base: ConversationState,
    deps: SourcingDeps,
) -> AgentOutcome:
    """Resolve a draft's customer and persist its source-aware order."""
    text = (message.text or "").strip()
    rag_client = deps.rag_client
    if rag_client is None:
        rag_client = getattr(deps.searcher, "client", None)
    create = parse_create_client_command(text)
    if not base.draft_items:
        if create is not None:
            return _handle_create_client(deps, base, *create)
        return AgentOutcome(state=base, reply=EMPTY_DRAFT_FINALIZE_REPLY)
    finalize_name = parse_finalize(text, base.draft_items)
    with deps.session_factory() as session:
        if create is not None and not base.customer_disambiguation_pending:
            return _create_customer_for_draft(session, base, *create, rag_client)
        if base.customer_disambiguation_pending:
            candidate = parse_customer_pick(text, base.customer_candidates)
            if candidate is None:
                return AgentOutcome(
                    state=base, reply=format_customer_menu(base.customer_candidates)
                )
            customer = session.get(Cliente, candidate.customer_id)
            if customer is None:
                return AgentOutcome(state=base, reply="The selected customer no longer exists.")
        elif base.customer_id is not None:
            customer = session.get(Cliente, base.customer_id)
            if customer is None:
                return AgentOutcome(state=base, reply="The session customer no longer exists.")
        else:
            if not finalize_name:
                return AgentOutcome(
                    state=base,
                    reply=ASK_CUSTOMER_FINALIZE_REPLY,
                )
            resolution = resolve_customer_name(session, finalize_name)
            if resolution.kind is CustomerResolutionKind.AMBIGUOUS:
                updated = base.with_updates(
                    customer_disambiguation_pending=True,
                    customer_candidates=resolution.candidates,
                )
                return AgentOutcome(
                    state=updated, reply=format_customer_menu(resolution.candidates)
                )
            if resolution.kind is CustomerResolutionKind.NOT_FOUND:
                return AgentOutcome(
                    state=base,
                    reply=(
                        f"I could not find customer «{finalize_name}». To create it on the Base list, "
                        "send: 'nuevo cliente <name> <phone>'."
                    ),
                )
            customer = resolution.candidate
        assert customer is not None
        return persist_finalized_draft(session, customer, base, rag_client)


def build_handler(
    responder: CustomerResponder,
    *,
    fallback_reply: str = GREETING,
    system_prompt: str = SYSTEM_PROMPT,
    searcher: ProductSearcher | None = None,
    sourcing: SourcingDeps | None = None,
) -> Callable[[InboundMessage, ConversationState | None, RoutingDecision], AgentOutcome]:
    """Build the Customer conversational handler around a mockable responder.

    Every user turn is answered by the LLM over the full context (system prompt
    + prior history + the new message); the greeting is only a fallback when
    the responder has no API key or the message carries no text. When a
    ``searcher`` is wired, the turn's text is resolved through the product-query
    chain and the source-discriminated result becomes a transient system note
    injected right before the user turn: it rides the outgoing message list only
    and never enters history. A SQLAlchemy error from the searcher skips the
    note so the conversation survives a down database.

    While ``product_options`` hold the last displayed results, an add-intent
    phrase ("agregalo", "sumá 5 de eso", "el 2", or a bare quantity answer
    such as "quiero 2") short-circuits the LLM and appends the referenced
    entry to ``draft_items`` even before an order exists. A finalize phrase
    then resolves the customer and persists the draft. ``sourcing`` provides
    the persistence boundaries (session factory + RAG client fallback) the
    finalize step shares with the guided flow.
    """

    def handler(
        message: InboundMessage,
        state: ConversationState | None,
        decision: RoutingDecision,
    ) -> AgentOutcome:
        history = state.history if state is not None else ()
        base = state if state is not None else ConversationState(sender_id=message.sender_id)
        text = (message.text or "").strip()
        # The remove-product command short-circuits before any add-intent or
        # LLM turn ("sacá el 2" must remove, never re-add the numbered option).
        remove_outcome = _run_remove_product_turn(message, base, sourcing)
        if remove_outcome is not None:
            return remove_outcome
        if sourcing is not None and (
            (base.customer_disambiguation_pending and base.draft_items)
            or is_finalize(text)
            or parse_create_client_command(text) is not None
        ):
            return _run_finalize_turn(message, base, sourcing)
        if not text:
            reply = fallback_reply
            new_history = (*history, ChatMessage("assistant", reply))
            updated = base.with_updates(history=new_history)
        else:
            intent = parse_product_add(text, base.product_options) if base.product_options else None
            if intent is not None:
                index, qty = intent
                entry = base.product_options[index]
                turn_history = (*history, ChatMessage("user", text))
                reply = format_added_to_order_reply(entry, qty)
                updated = base.with_updates(
                    history=(*turn_history, ChatMessage("assistant", reply)),
                    product_options=(),
                    draft_items=(*base.draft_items, (entry, qty)),
                )
                return AgentOutcome(state=updated, reply=reply)
            messages = [ChatMessage("system", system_prompt), *history]
            displayed: tuple[ProductEntry, ...] = ()
            if searcher is not None:
                try:
                    result = searcher.search(text)
                except SQLAlchemyError:
                    logger.warning(
                        "product search failed for query=%r; answering without catalog context",
                        text,
                    )
                else:
                    draft_entries = tuple(entry for entry, _ in base.draft_items)
                    messages.append(
                        ChatMessage(
                            "system",
                            product_context_note(text, result, draft=draft_entries),
                        )
                    )
                    displayed = result.entries
            messages.append(ChatMessage("user", text))
            try:
                reply = responder.respond(messages)
            except ResponderNotConfigured:
                reply = fallback_reply
            new_history = (*history, ChatMessage("user", text), ChatMessage("assistant", reply))
            updated = base.with_updates(history=new_history)
            # A turn that displays nothing must not clear the last displayed
            # product, or "agregale N" and bare-quantity adds die after any
            # unrecognized message.
            if displayed:
                updated = updated.with_updates(product_options=displayed)
        return AgentOutcome(state=updated, reply=reply)

    return handler
