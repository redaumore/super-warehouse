"""Guided (scripted) order-creation agent: the system asks, the owner answers.

After a session reset ("hola bob") the SYSTEM takes command with a fixed
question sequence instead of inviting free-form chat:

1. ask_client — "¿Para qué cliente querés armar un pedido?" (numbered
   disambiguation menu on an ambiguous name; in-chat client creation via the
   existing ``nuevo cliente`` command);
2. ask_product — "¿Qué producto querés agregar al pedido?" (product search;
   a single hit goes straight to the quantity question, several hits render a
   numbered pick list one per line);
3. ask_quantity — a positive quantity adds the product to the draft;
4. ask_more — "¿Querés agregar otro producto?" (sí loops back to 2, no
   finalizes);
5. finalize — the draft is persisted through the SAME path as the free-form
   finalize (``persist_finalized_draft``), the multi-line quote is shown, and
   ``awaiting_decision`` hands the "aprobá"/"rechazá" turn to Dispatch.

The handler is fully deterministic: it never calls the LLM. An unparseable
answer re-asks the current question with a brief hint. Guided-flow
bookkeeping lives on ``ConversationState.guided_step`` /
``guided_product_options`` / ``guided_product`` and is deliberately not
rehydrated from the DB: an expired flow just restarts with "hola bob".
"""

from __future__ import annotations

import re
from collections.abc import Callable

from src.agents.commands import GUIDED_ASK_CLIENT, GUIDED_ASK_MORE, GUIDED_ASK_PRODUCT
from src.agents.customer import SourcingDeps, persist_finalized_draft
from src.agents.customers import (
    CustomerResolutionKind,
    format_customer_menu,
    parse_create_client_command,
    parse_customer_pick,
    resolve_customer_name,
)
from src.agents.disambiguation import normalize_text
from src.agents.product_search import ProductEntry, ProductSearcher, ProductSource
from src.channels.base import InboundMessage
from src.db.models import Cliente
from src.orchestrator.router import AgentOutcome, RoutingDecision
from src.orchestrator.session import ConversationState

# Pragmatic sí/no matching over the normalized (lowercase, accent-free,
# punctuation-free) answer. "dale" accepts another product; "listo"/
# "nada más" close the draft. Whole-answer match keeps a stray product name
# from being read as a decision.
_YES_ANSWERS = {"si", "s", "dale", "ok", "obvio", "claro", "vamos", "dale si"}
_NO_ANSWERS = {
    "no",
    "nop",
    "listo",
    "nada mas",
    "no nada mas",
    "ya esta",
    "eso es todo",
    "suficiente",
}

# Quantity answer: an optional permissive verb prefix, a positive integer
# (digits), an optional "unidades"/"u" suffix, nothing else. Anchored so a
# product name in the same message is re-asked, never guessed.
_QUANTITY_RE = re.compile(
    r"^\s*(?:(?:quiero|dame|anotame|llevame|lleva|llevo|necesito|son|serian|van)\s+)?"
    r"(\d+)\s*(?:unidades|u|uds)?\s*[.!]*$"
)


def _yes_no_answer(text: str) -> bool | None:
    """Classify a sí/no answer; ``None`` when the text is neither."""
    folded = normalize_text(text)
    if not folded:
        return None
    if folded in _NO_ANSWERS:
        return False
    if folded in _YES_ANSWERS:
        return True
    return None


def _parse_quantity(text: str) -> int | None:
    """Parse a positive quantity answer; ``None`` when unparseable or non-positive."""
    match = _QUANTITY_RE.match(text or "")
    if match is None:
        return None
    quantity = int(match.group(1))
    return quantity if quantity > 0 else None


def _parse_numbered_pick(text: str, options_count: int) -> int | None:
    """Map a 1-based numeric pick to an index; ``None`` when out of range."""
    raw = (text or "").strip()
    if not raw.isdigit():
        return None
    index = int(raw) - 1
    return index if 0 <= index < options_count else None


def _format_product_options(entries: tuple[ProductEntry, ...]) -> str:
    """Render the numbered product pick list, one product per line."""
    lines = ["Encontré varios; respondé el número:"]
    lines.extend(f"{i}. {entry.name}" for i, entry in enumerate(entries, start=1))
    return "\n".join(lines)


def build_guided_handler(
    sourcing: SourcingDeps | None,
    *,
    searcher: ProductSearcher | None = None,
) -> Callable[[InboundMessage, ConversationState | None, RoutingDecision], AgentOutcome]:
    """Build the guided (scripted) order-creation handler.

    ``sourcing`` provides the persistence boundaries for the finalize step
    (session factory + supplier searcher + optional RAG client) — the same
    ``SourcingDeps`` the Customer agent uses, so both flows share ONE draft
    persistence path. ``searcher`` is the product-query seam for the product
    step. With either boundary missing the flow answers deterministically
    (no LLM) and tells the owner the step cannot run.
    """

    def handler(
        message: InboundMessage,
        state: ConversationState | None,
        decision: RoutingDecision,
    ) -> AgentOutcome:
        base = state if state is not None else ConversationState(sender_id=message.sender_id)
        text = (message.text or "").strip()
        step = base.guided_step or "ask_client"
        if step == "ask_client":
            return _run_ask_client(message, base, text, sourcing)
        if step == "ask_product":
            return _run_ask_product(base, text, searcher)
        if step == "ask_quantity":
            return _run_ask_quantity(base, text)
        return _run_ask_more(base, text, sourcing)

    return handler


def _run_ask_client(
    message: InboundMessage,
    base: ConversationState,
    text: str,
    sourcing: SourcingDeps | None,
) -> AgentOutcome:
    """Resolve the client name answer (or its numbered-menu pick) and advance."""
    # A numbered menu is pending: only a valid pick advances (the owner may
    # also retype the name, which falls through to a fresh resolution).
    if base.customer_candidates:
        pick = parse_customer_pick(text, base.customer_candidates)
        if pick is not None:
            updated = base.with_updates(
                customer_id=pick.customer_id,
                customer_candidates=(),
                customer_disambiguation_pending=False,
                guided_step="ask_product",
            )
            return AgentOutcome(state=updated, reply=GUIDED_ASK_PRODUCT)
        if text.isdigit():
            return AgentOutcome(state=base, reply=format_customer_menu(base.customer_candidates))
    # In-chat client creation intercept (reuses the Customer agent's command).
    if sourcing is not None:
        create = parse_create_client_command(text)
        if create is not None:
            from src.agents.customer import _handle_create_client

            outcome = _handle_create_client(sourcing, base, *create)
            assert outcome.state is not None
            return AgentOutcome(
                state=outcome.state.with_updates(guided_step="ask_client"),
                reply=outcome.reply,
            )
    if not text:
        return AgentOutcome(state=base, reply=GUIDED_ASK_CLIENT)
    if sourcing is None:
        return AgentOutcome(state=base, reply=GUIDED_ASK_CLIENT)
    with sourcing.session_factory() as session:
        resolution = resolve_customer_name(session, text)
        if resolution.kind is CustomerResolutionKind.AMBIGUOUS:
            updated = base.with_updates(
                customer_candidates=resolution.candidates,
                customer_disambiguation_pending=True,
                guided_step="ask_client",
            )
            return AgentOutcome(state=updated, reply=format_customer_menu(resolution.candidates))
        if resolution.kind is CustomerResolutionKind.NOT_FOUND:
            return AgentOutcome(
                state=base,
                reply=(
                    f"No encontré ningún cliente llamado «{text}». "
                    "Si es nuevo, mandá: 'nuevo cliente <nombre> <teléfono>'."
                ),
            )
        assert resolution.candidate is not None
        updated = base.with_updates(
            customer_id=resolution.candidate.customer_id,
            customer_candidates=(),
            customer_disambiguation_pending=False,
            guided_step="ask_product",
        )
        return AgentOutcome(state=updated, reply=GUIDED_ASK_PRODUCT)


def _run_ask_product(
    base: ConversationState,
    text: str,
    searcher: ProductSearcher | None,
) -> AgentOutcome:
    """Run the product search (or its numbered pick) and ask for the quantity."""
    if base.guided_product_options:
        index = _parse_numbered_pick(text, len(base.guided_product_options))
        if index is None:
            return AgentOutcome(
                state=base,
                reply=_format_product_options(base.guided_product_options),
            )
        entry = base.guided_product_options[index]
        updated = base.with_updates(
            guided_product_options=(), guided_product=entry, guided_step="ask_quantity"
        )
        return AgentOutcome(
            state=updated, reply=f"¿Cuántas unidades de {entry.name} querés agregar?"
        )
    if not text:
        return AgentOutcome(state=base, reply=GUIDED_ASK_PRODUCT)
    if searcher is None:
        return AgentOutcome(
            state=base,
            reply="No tengo catálogo disponible ahora; probá de nuevo más tarde.",
        )
    result = searcher.search(text)
    if result.source is ProductSource.NONE:
        return AgentOutcome(
            state=base,
            reply=f"No encontré «{text}» en los catálogos. Probá con otro nombre.",
        )
    if result.source is ProductSource.ERROR:
        return AgentOutcome(
            state=base,
            reply="No pude consultar los catálogos ahora. Probá de nuevo en un rato.",
        )
    if len(result.entries) == 1:
        entry = result.entries[0]
        updated = base.with_updates(guided_product=entry, guided_step="ask_quantity")
        return AgentOutcome(
            state=updated, reply=f"¿Cuántas unidades de {entry.name} querés agregar?"
        )
    updated = base.with_updates(guided_product_options=result.entries, guided_step="ask_product")
    return AgentOutcome(state=updated, reply=_format_product_options(result.entries))


def _run_ask_quantity(base: ConversationState, text: str) -> AgentOutcome:
    """Add the pending product with the answered quantity and ask for more."""
    quantity = _parse_quantity(text)
    if quantity is None or base.guided_product is None:
        return AgentOutcome(state=base, reply="Decime una cantidad mayor a 0, por ejemplo '3'.")
    entry = base.guided_product
    updated = base.with_updates(
        draft_items=(*base.draft_items, (entry, quantity)),
        guided_product=None,
        guided_step="ask_more",
    )
    reply = f"Listo: agregué {quantity} × {entry.name}.\n{GUIDED_ASK_MORE}"
    return AgentOutcome(state=updated, reply=reply)


def _run_ask_more(
    base: ConversationState,
    text: str,
    sourcing: SourcingDeps | None,
) -> AgentOutcome:
    """Loop back to the product question, or finalize the draft for confirmation."""
    answer = _yes_no_answer(text)
    if answer is None:
        return AgentOutcome(state=base, reply=f"Respondé 'sí' o 'no'. {GUIDED_ASK_MORE}")
    if answer:
        return AgentOutcome(
            state=base.with_updates(guided_step="ask_product"), reply=GUIDED_ASK_PRODUCT
        )
    return _finalize_guided_draft(base, sourcing)


def _finalize_guided_draft(
    base: ConversationState,
    sourcing: SourcingDeps | None,
) -> AgentOutcome:
    """Persist the guided draft via the shared finalize path and hand over to Dispatch.

    On success the reply is the multi-line quote and ``awaiting_decision``
    routes the owner's "aprobá"/"rechazá" to DISPATCH; ``guided_step`` is
    cleared so the scripted flow is closed either way.
    """
    if sourcing is None or not base.draft_items or base.customer_id is None:
        # Degraded or empty flow: close the script and restart from the client.
        restarted = base.with_updates(
            guided_step="ask_client", guided_product=None, guided_product_options=()
        )
        return AgentOutcome(state=restarted, reply=GUIDED_ASK_CLIENT)
    rag_client = sourcing.rag_client or getattr(sourcing.searcher, "client", None)
    with sourcing.session_factory() as session:
        customer = session.get(Cliente, base.customer_id)
        if customer is None:
            restarted = base.with_updates(
                guided_step="ask_client", guided_product=None, guided_product_options=()
            )
            return AgentOutcome(state=restarted, reply=GUIDED_ASK_CLIENT)
        outcome = persist_finalized_draft(session, customer, base, rag_client)
        assert outcome.state is not None
        closed = outcome.state.with_updates(
            guided_step=None, guided_product=None, guided_product_options=()
        )
        return AgentOutcome(state=closed, reply=outcome.reply)
