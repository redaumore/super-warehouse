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
the owner to finalize with "cerrá el pedido para <cliente>". Without an open
order the handler offers to create one through the existing sourcing path.

The sourcing turn (parsed orders) resolves the customer by NAME
(``src/agents/customers.py``), offers in-chat creation for unknown names and
runs the Case A/B/C flows; quotes and cancellations are in-chat replies.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, TypedDict

import phonenumbers
from phonenumbers import PhoneNumber, PhoneNumberFormat
from phonenumbers.phonenumberutil import NumberParseException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
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
    ResolutionKind,
    SearchCandidate,
    normalize_text,
    resolve_item,
    search_catalog,
)
from src.agents.intake import ParsedItem
from src.agents.inventory import available_stock
from src.agents.product_search import (
    ProductEntry,
    ProductSearcher,
    ProductSearchResult,
    ProductSource,
    is_finalize,
    parse_finalize,
    parse_product_add,
)
from src.agents.sales import Quote
from src.channels.base import InboundMessage
from src.db.models import AppSetting, Catalogo, Cliente, ExchangeRate, Order, Supplier
from src.db.session import SessionLocal
from src.integrations.rag import RagProductClient
from src.orchestrator.router import AgentOutcome, RoutingDecision
from src.orchestrator.session import ChatMessage, ConversationState, ResolvedItem, SourcingNeedItem
from src.pricing.order_pricing import (
    MarginSource,
    MissingRateError,
    PricedOrder,
    PricingLine,
    RateSource,
    compute_order,
    pending_order,
)
from src.sourcing.case_a import persist_case_a_order
from src.sourcing.classify import MissingItem, SourcingCase, classify_case
from src.sourcing.draft_order import persist_draft_order
from src.supplier.searcher import SupplierCatalogSearcher

logger = logging.getLogger(__name__)

_DEFAULT_REGION = "AR"

# Fallback reply when the LLM responder is unavailable.
GREETING = "¿Qué pedido cargamos hoy? Decime el cliente, los productos y la cantidad."

# Add-intent short-circuit replies (owner-facing, rioplatense).
OFFER_TO_CREATE_REPLY = (
    "Todavía no hay un pedido abierto para agregar productos. "
    "Mandá el pedido completo (cliente, productos y cantidades) y lo cargo."
)
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


def format_case_a_reply(
    order: Order, quote: Quote, delivery_date: date | None, customer_name: str | None = None
) -> str:
    """Owner confirmation for a full-stock (Case A) order, in the owner's chat."""
    who = f" de {customer_name}" if customer_name else ""
    date_part = f" Fecha estimada de entrega: {delivery_date.isoformat()}." if delivery_date else ""
    return (
        f"Pedido #{order.order_id}{who} confirmado, tenemos todo el stock. "
        f"Total estimado: {quote.total:.2f} ARS.{date_part} "
        "¿Lo aprobás? Respondé 'aprobá' o 'rechazá'."
    )


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


def _resolve_items(
    session: Session, parsed_items: Sequence[ParsedItem]
) -> tuple[ResolvedItem, ...]:
    """Resolve parsed descriptions to catalog SKUs; unknown items stay missing."""
    resolved: list[ResolvedItem] = []
    for item in parsed_items:
        resolution = resolve_item(session, item.description)
        if resolution.kind is ResolutionKind.AUTO_MAPPED and resolution.candidate is not None:
            resolved.append(
                ResolvedItem(
                    sku=resolution.candidate.sku,
                    cantidad=item.quantity,
                    description=item.description,
                )
            )
        else:
            resolved.append(
                ResolvedItem(
                    sku=normalize_text(item.description),
                    cantidad=item.quantity,
                    description=item.description,
                )
            )
    return tuple(resolved)


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
                state=base.with_updates(parsed_order=None),
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
                state=base.with_updates(parsed_order=None),
                reply=f"No pude crear el cliente: {exc}",
            )
        session.commit()
    reply = f"Listo: di de alta a {client.nombre_comercial}. Ahora mandá el pedido con su nombre."
    return AgentOutcome(state=base.with_updates(parsed_order=None), reply=reply)


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
        return session.scalar(
            select(ExchangeRate.rate_to_ars).where(ExchangeRate.currency == code)
        )

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
            product = session.scalar(
                select(Catalogo).where(Catalogo.codigo_interno == entry.sku)
            )
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
                supplier=entry.provider or code,
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
    """Render the owner-facing quote for a newly persisted draft."""
    if priced.conversion_pending:
        return (
            f"Pedido #{order.order_id} para {customer.nombre_comercial}: conversión pendiente. "
            "Cargá el tipo de cambio que falta en Customer Orders y aprobalo."
        )
    lines = " ".join(
        f"{line.cantidad} × {line.name or line.sku}: {line.final_ars:.2f} ARS"
        for line in priced.lines
    )
    return (
        f"Pedido #{order.order_id} para {customer.nombre_comercial} — "
        f"total {priced.total:.2f} ARS. {lines} "
        f"Respondé '{APPROVE}' o '{REJECT}'."
    )


def _persist_finalized_draft(
    session: Session,
    customer: Cliente,
    base: ConversationState,
    rag_client: RagProductClient | None,
) -> AgentOutcome:
    """Price, persist, and close a draft for a resolved customer."""
    try:
        priced = _price_draft(session, customer, base, rag_client)
    except DraftPricingError as exc:
        return AgentOutcome(state=base, reply=f"I could not price the draft: {exc}")
    order = persist_draft_order(session, customer, priced)
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
        parsed_order=None,
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
        return AgentOutcome(state=base, reply=f"I could not create the customer: invalid phone {telefono}")
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
    return _persist_finalized_draft(session, customer, base, rag_client)


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
                return AgentOutcome(state=base, reply=format_customer_menu(base.customer_candidates))
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
                return AgentOutcome(state=updated, reply=format_customer_menu(resolution.candidates))
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
        return _persist_finalized_draft(session, customer, base, rag_client)


def _run_sourcing_turn(
    message: InboundMessage,
    state: ConversationState | None,
    decision: RoutingDecision,
    deps: SourcingDeps,
) -> AgentOutcome:
    """Handle a parsed order turn: resolve the customer and run the matching case flow.

    The customer is resolved by NAME (never by sender phone): one match
    auto-selects, two or more show a numbered disambiguation menu (the pick
    arrives on a later turn), zero offers in-chat creation. ``nuevo cliente
    <nombre> <teléfono>`` is intercepted before any order handling.
    """
    parsed = state.parsed_order if state is not None else None
    base = state if state is not None else ConversationState(sender_id=message.sender_id)
    text = (message.text or "").strip()

    # In-chat client creation intercept: runs before order parsing so the
    # command never falls into the "specify products" branch.
    create = parse_create_client_command(text)
    if create is not None:
        return _handle_create_client(deps, base, *create)

    if parsed is None or not parsed.items:
        reply = (
            "¿Qué pedido cargamos? Decime el nombre del cliente, "
            "el artículo y la cantidad, por ejemplo 'para Don Juan, 10 clavos'."
        )
        return AgentOutcome(state=base.with_updates(parsed_order=None), reply=reply)

    with deps.session_factory() as session:
        if base.customer_disambiguation_pending:
            # The owner picks from the numbered menu; the order waits in the
            # state (parsed_order was kept for this turn).
            candidate = parse_customer_pick(text, base.customer_candidates)
            if candidate is None:
                return AgentOutcome(
                    state=base, reply=format_customer_menu(base.customer_candidates)
                )
            customer = session.get(Cliente, candidate.customer_id)
            assert customer is not None
            pending_cleared = base.with_updates(
                customer_disambiguation_pending=False, customer_candidates=()
            )
        else:
            name = parsed.customer_name
            if not name:
                reply = (
                    "¿Para qué cliente es el pedido? Decime el nombre "
                    "(o 'nuevo cliente <nombre> <teléfono>' si es nuevo) y los productos."
                )
                return AgentOutcome(state=base.with_updates(parsed_order=None), reply=reply)
            resolution = resolve_customer_name(session, name)
            if resolution.kind is CustomerResolutionKind.AMBIGUOUS:
                reply = format_customer_menu(resolution.candidates)
                updated = base.with_updates(
                    customer_disambiguation_pending=True,
                    customer_candidates=resolution.candidates,
                    parsed_order=parsed,  # kept for the pick turn
                )
                return AgentOutcome(state=updated, reply=reply)
            if resolution.kind is CustomerResolutionKind.NOT_FOUND:
                reply = (
                    f"No encontré ningún cliente llamado «{name}». "
                    "Si es nuevo, mandá: 'nuevo cliente <nombre> <teléfono>'."
                )
                return AgentOutcome(state=base.with_updates(parsed_order=None), reply=reply)
            customer = resolution.candidate
            assert customer is not None
            pending_cleared = base.with_updates(
                customer_disambiguation_pending=False, customer_candidates=()
            )

        resolved = _resolve_items(session, parsed.items)
        sourcing = classify_case(resolved, lambda sku: available_stock(session, sku), deps.searcher)
        if sourcing.case is SourcingCase.A:
            order, quote = persist_case_a_order(
                session, customer, resolved, delivery_date=parsed.delivery_date
            )
            reply = format_case_a_reply(
                order, quote, parsed.delivery_date, customer.nombre_comercial
            )
            updated = pending_cleared.with_updates(
                customer_id=customer.customer_id,
                order_id=order.order_id,
                items=tuple(resolved),
                awaiting_decision=True,
                parsed_order=None,
            )
        elif sourcing.case is SourcingCase.B:
            from src.sourcing.case_b import persist_case_b_order

            order = persist_case_b_order(
                session, customer, delivery_date=parsed.delivery_date, missing=sourcing.missing
            )
            needs = tuple(
                SourcingNeedItem(sku=m.sku, missing_quantity=m.missing_quantity)
                for m in sourcing.missing
            )
            candidates = tuple(c for m in sourcing.missing for c in m.candidates)
            reply = format_case_b_reply(order, sourcing.missing)
            updated = pending_cleared.with_updates(
                customer_id=customer.customer_id,
                order_id=order.order_id,
                items=tuple(resolved),
                parsed_order=None,
                sourcing_selection_pending=True,
                sourcing_needs=needs,
                sourcing_candidates=candidates,
            )
        else:
            from src.sourcing.case_c import cancel_for_no_supplier, persist_case_c_order

            order = persist_case_c_order(session, customer, delivery_date=parsed.delivery_date)
            cancel_for_no_supplier(session, order)
            reply = format_case_c_reply(order, sourcing.missing, customer.nombre_comercial)
            updated = pending_cleared.with_updates(
                customer_id=customer.customer_id,
                order_id=order.order_id,
                items=tuple(resolved),
                parsed_order=None,
            )
        # The sourcing turn owns its transaction: persist the flow's writes so
        # they survive the session close (the pipeline runs fire-and-forget).
        session.commit()
        return AgentOutcome(state=updated, reply=reply)


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
    then resolves the customer and persists the draft.

    When ``sourcing`` is wired and the orchestrator's parse step flagged the
    turn (``decision.parsed``), the handler runs the sourcing workflow instead
    of the LLM chat: the parsed order is classified into Case A/B/C and the
    matching flow persists the order, reserves stock, lists suppliers or
    cancels — per the order-sourcing spec.
    """

    def handler(
        message: InboundMessage,
        state: ConversationState | None,
        decision: RoutingDecision,
    ) -> AgentOutcome:
        history = state.history if state is not None else ()
        base = state if state is not None else ConversationState(sender_id=message.sender_id)
        text = (message.text or "").strip()
        if sourcing is not None and (
            (base.customer_disambiguation_pending and base.draft_items)
            or is_finalize(text)
            or parse_create_client_command(text) is not None
        ):
            return _run_finalize_turn(message, base, sourcing)
        if decision.parsed and sourcing is not None:
            return _run_sourcing_turn(message, state, decision, sourcing)
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
