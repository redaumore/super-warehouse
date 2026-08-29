"""Customer agent: owner-assistant catalog context and conversational replies.

The owner is the only chat actor: the persona is the owner's assistant, not a
customer-facing chatbot. Every text turn is answered by the LLM responder over
the full context (system prompt + prior history + the new message). Slice 2
adds a catalog search boundary: when a ``CatalogSearcher`` is wired, the
turn's text is searched against `catalogo` and the results become a TRANSIENT
system note injected right before the user turn — never persisted into
history.

Because the catalog is currently empty, a product query returns no candidates
and the note instructs the assistant to tell the owner the product is not in
stock. A database error skips the note instead of failing the turn, so the
conversation keeps answering while the DB is down.

The sourcing turn (parsed orders) resolves the customer by NAME
(``src/agents/customers.py``), offers in-chat creation for unknown names and
runs the Case A/B/C flows; quotes and cancellations are in-chat replies.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import phonenumbers
from phonenumbers import PhoneNumber, PhoneNumberFormat
from phonenumbers.phonenumberutil import NumberParseException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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
from src.agents.sales import Quote
from src.channels.base import InboundMessage
from src.db.models import Cliente, Order
from src.db.session import SessionLocal
from src.orchestrator.router import AgentOutcome, RoutingDecision
from src.orchestrator.session import ChatMessage, ConversationState, ResolvedItem, SourcingNeedItem
from src.sourcing.case_a import persist_case_a_order
from src.sourcing.classify import MissingItem, SourcingCase, classify_case
from src.supplier.searcher import SupplierCatalogSearcher

logger = logging.getLogger(__name__)

_DEFAULT_REGION = "AR"

# Fallback reply when the LLM responder is unavailable.
GREETING = "¿Qué pedido cargamos hoy? Decime el cliente, los productos y la cantidad."

SYSTEM_PROMPT = (
    "Sos el asistente del dueño de una ferretería de barrio, y te escribe el dueño "
    "por WhatsApp o Telegram. Tu trabajo es cargar los pedidos de sus clientes: "
    "primero identificás al cliente por nombre, después los productos y cantidades. "
    "Respondé en español rioplatense, con tono cálido y directo, en mensajes cortos."
)


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


class CatalogSearcher(Protocol):
    """Search boundary over the product catalog (real impl: DbCatalogSearcher)."""

    def search(self, query: str) -> tuple[SearchCandidate, ...]:
        """Return catalog candidates for ``query``, best first."""


class DbCatalogSearcher:
    """Catalog search backed by Postgres; one short-lived session per call.

    The with-block closes the session even when the query raises, so the
    Customer agent never leaks connections across turns.
    """

    def search(self, query: str) -> tuple[SearchCandidate, ...]:
        """Open a session, run the hybrid catalog search, return candidates."""
        with SessionLocal() as session:
            return tuple(search_catalog(session, query, limit=3))


def catalog_context_note(query: str, candidates: Sequence[SearchCandidate]) -> str:
    """Render catalog search results as a transient system note (Spanish, rioplatense).

    The note is product copy for the owner's assistant: an empty result set
    tells it the product is not in stock; candidates list the products the
    reply must use.
    """
    if not candidates:
        return (
            f"Búsqueda en catálogo para «{query}»: sin resultados. "
            "Si el cliente pidió un producto que no está en stock, decíselo al dueño."
        )
    items = ", ".join(f"{c.nombre_oficial} ({c.sku})" for c in candidates)
    return (
        f"Búsqueda en catálogo para «{query}»: {len(candidates)} resultado(s): {items}. "
        "Respondé usando estos productos."
    )


@dataclass
class SourcingDeps:
    """Boundaries the sourcing turn needs (session + supplier searcher).

    Quotes, cancellations and approvals are in-chat replies, so no notifier or
    owner phone is bridged anymore — the pipeline edge owns the only outbound.
    """

    session_factory: Callable[[], Session]
    searcher: SupplierCatalogSearcher


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
            "Elegí proveedor para cada uno (respondé los números):"
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
    searcher: CatalogSearcher | None = None,
    sourcing: SourcingDeps | None = None,
) -> Callable[[InboundMessage, ConversationState | None, RoutingDecision], AgentOutcome]:
    """Build the Customer conversational handler around a mockable responder.

    Every user turn is answered by the LLM over the full context (system prompt
    + prior history + the new message); the greeting is only a fallback when
    the responder has no API key or the message carries no text. When a
    ``searcher`` is wired, catalog results for the turn's text become a
    transient system note injected right before the user turn: it rides the
    outgoing message list only and never enters history (which keeps user and
    assistant turns). A SQLAlchemy error skips the note so the conversation
    survives a down database.

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
        if decision.parsed and sourcing is not None:
            return _run_sourcing_turn(message, state, decision, sourcing)
        history = state.history if state is not None else ()
        base = state if state is not None else ConversationState(sender_id=message.sender_id)
        text = (message.text or "").strip()
        if not text:
            reply = fallback_reply
            new_history = (*history, ChatMessage("assistant", reply))
        else:
            messages = [ChatMessage("system", system_prompt), *history]
            if searcher is not None:
                try:
                    candidates = searcher.search(text)
                except SQLAlchemyError:
                    logger.warning(
                        "catalog search failed for query=%r; answering without catalog context",
                        text,
                    )
                else:
                    messages.append(ChatMessage("system", catalog_context_note(text, candidates)))
            messages.append(ChatMessage("user", text))
            try:
                reply = responder.respond(messages)
            except ResponderNotConfigured:
                reply = fallback_reply
            new_history = (*history, ChatMessage("user", text), ChatMessage("assistant", reply))
        return AgentOutcome(state=base.with_updates(history=new_history), reply=reply)

    return handler
