"""Walking-skeleton pipeline: owner gate + orchestrator + agent handlers.

Composes the real ``Orchestrator`` (router + in-memory session store) with
agent handlers and sends a reply back on the inbound channel, so a message
round-trips end-to-end through routing and persistence. The owner is the only
chat actor: every inbound message is gated at the pipeline edge
(``src/orchestrator/owner.py``) BEFORE routing; non-owner senders get a polite
rejection and are never routed. Customer replies come from the LLM responder
(OpenAI, greeting fallback when unconfigured) and each text turn is resolved
through the local-first → RAG-fallback product searcher
(``PrecedenceProductSearcher``: local ``DbCatalogSearcher`` first, supplier
catalog ``RagProductClient`` on empty local results), which degrades
gracefully — no catalog note — when the local database is down.

When an owner sender key is configured, the order-sourcing workflow is
enabled: the parse step extracts structured order fields (customer name, items,
delivery date) before the Customer agent, the Customer handler resolves the
customer by name and classifies each order into Case A/B/C, the SOURCING agent
handles the owner's supplier-selection replies, and the DISPATCH agent runs the
real approval flow (``parse_decision`` → ``apply_decision`` →
``register_approved_order`` with the ``SheetsWriter``). The conversation store
is wired to rehydrate expired conversations from the database (latest open
order across customers), so multi-turn flows survive the 30-minute in-memory
TTL. Clearing both owner keys disables the parse step and keeps the legacy
conversational intake.

Quotes, cancellations and approvals are IN-CHAT replies: the old
``_ChannelNotifier`` owner push (``owner_phone``) is gone — the pipeline edge
owns the single outbound send. The webhook ACKs before this background handler
runs, so the 5-second SLA is never affected.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from src.agents.customer import (
    CustomerResponder,
    DbCatalogSearcher,
    SourcingDeps,
    build_handler,
)
from src.agents.dispatch import build_dispatch_handler
from src.agents.intake import OrderParser, SimpleOrderParser
from src.agents.product_search import PrecedenceProductSearcher, ProductSearcher
from src.channels import CHANNELS
from src.channels.base import InboundMessage
from src.config import get_settings
from src.db.session import SessionLocal
from src.integrations.openai import OpenAIResponder
from src.integrations.rag import RagProductClient
from src.integrations.sheets import SheetsWriter
from src.orchestrator.owner import is_owner_sender, rejection_reply
from src.orchestrator.router import (
    AgentName,
    AgentOutcome,
    Orchestrator,
    RoutingDecision,
)
from src.orchestrator.session import ConversationState, ConversationStore, rehydrate_conversation
from src.sourcing.case_b import build_sourcing_handler
from src.supplier.searcher import FakeSupplierCatalogSearcher

logger = logging.getLogger(__name__)


def _stub_agent(
    message: InboundMessage,
    state: ConversationState | None,
    _decision: RoutingDecision,
) -> AgentOutcome:
    """Walking-skeleton agent stub: no domain work — preserve (or seed) context."""
    if state is not None:
        return AgentOutcome(state=state.with_updates())
    return AgentOutcome(state=ConversationState(sender_id=message.sender_id))


def _sourcing_deps() -> SourcingDeps | None:
    """Wire the sourcing boundaries, or ``None`` when the flow is disabled.

    The supplier searcher is the in-memory fake (empty candidate list) until
    the external RAG exists: every missing item then classifies as Case C —
    the safe degraded behavior — and the owner swaps in a real searcher later.
    The flow is enabled by configuring an owner sender key (either channel);
    with both keys empty the legacy intake keeps working.
    """
    settings = get_settings()
    if not (settings.owner_telegram_chat_id or settings.owner_whatsapp_phone):
        return None
    rag_client = RagProductClient()
    return SourcingDeps(
        session_factory=SessionLocal,
        searcher=FakeSupplierCatalogSearcher(),
        rag_client=rag_client,
    )


def build_orchestrator(
    responder: CustomerResponder | None = None,
    searcher: ProductSearcher | None = None,
    *,
    sourcing: SourcingDeps | None = None,
    parser: OrderParser | None = None,
    dispatch: Callable[..., AgentOutcome | None] | None = None,
    sheets: SheetsWriter | None = None,
) -> Orchestrator:
    """Build the app's orchestrator.

    Customer is wired to the real OpenAI-backed responder (greeting fallback
    when unconfigured) plus the local-first → RAG-fallback product searcher
    (``PrecedenceProductSearcher`` over ``DbCatalogSearcher`` + the supplier
    catalog ``RagProductClient``); the other agents stay walking-skeleton stubs.
    With ``sourcing`` wired, the parse step, the SOURCING confirm agent and the
    wired DISPATCH approval flow are enabled, and the store rehydrates expired
    conversations from the database. ``dispatch``/``sheets`` are injectable for
    tests; production uses ``build_dispatch_handler(SessionLocal, SheetsWriter())``.
    """
    rehydrator = None
    if sourcing is not None:
        searcher_ref = sourcing.searcher

        def _db_rehydrate(sender_id: str) -> ConversationState | None:
            with SessionLocal() as session:
                return rehydrate_conversation(session, sender_id, searcher=searcher_ref)

        rehydrator = _db_rehydrate
    orchestrator = Orchestrator(ConversationStore(rehydrator=rehydrator), parser=parser)
    for agent in AgentName:
        orchestrator.register(agent, _stub_agent)
    orchestrator.register(
        AgentName.CUSTOMER,
        build_handler(
            responder or OpenAIResponder(),
            searcher=(
                searcher
                if searcher is not None
                else PrecedenceProductSearcher(
                    DbCatalogSearcher(),
                    (sourcing.rag_client if sourcing else None) or RagProductClient(),
                )
            ),
            sourcing=sourcing,
        ),
    )
    if sourcing is not None:
        orchestrator.register(AgentName.SOURCING, build_sourcing_handler(SessionLocal))
        dispatcher = (
            dispatch
            if dispatch is not None
            else build_dispatch_handler(SessionLocal, sheets or SheetsWriter())
        )
        orchestrator.register(AgentName.DISPATCH, dispatcher)
    return orchestrator


def _default_sourcing() -> tuple[SourcingDeps | None, OrderParser | None]:
    """Resolve the enabled sourcing flow: deps + parser, or legacy (None, None)."""
    deps = _sourcing_deps()
    parser = SimpleOrderParser() if deps is not None else None
    return deps, parser


# The single orchestrator instance the intake dispatches to. Its session store
# is in-memory (a persistent store is a later concern; see docs/architecture.md).
_DEFAULT_DEPS, _DEFAULT_PARSER = _default_sourcing()
ORCHESTRATOR: Orchestrator = build_orchestrator(
    sourcing=_DEFAULT_DEPS,
    parser=_DEFAULT_PARSER,
)


def _reply_for(message: InboundMessage, decision: RoutingDecision, reply: str | None) -> str:
    """Compose the walking-skeleton reply for one turn; the agent's reply wins, echo is fallback."""
    if reply is not None:
        return reply
    if decision.media_kind == "voice":
        return "Recibí tu nota de voz (transcripción pendiente)."
    if decision.media_kind == "image":
        return "Recibí tu imagen (análisis pendiente)."
    stage = "continuando el pedido" if decision.context_loaded else "pedido nuevo"
    text = (message.text or "").strip()
    if text:
        return f"[orquestador] {decision.agent.value} · {stage}: {text}"
    return f"[orquestador] {decision.agent.value} · {stage}"


async def handle_inbound(message: InboundMessage) -> None:
    """Route one inbound message through the orchestrator and reply on its channel.

    The owner gate runs FIRST, at the pipeline edge: a non-owner sender gets a
    polite rejection and is never routed — no order is created, quoted or
    approved for them. The webhook already ACKed before this background task
    runs, so the gate never delays the 5-second SLA.
    """
    settings = get_settings()
    adapter = CHANNELS.get(message.channel)
    if not is_owner_sender(message.sender_id, message.channel, settings):
        if adapter is None:
            logger.warning("no adapter for channel=%s; rejection dropped", message.channel)
            return
        try:
            await adapter.send_text(message.sender_id, rejection_reply())
        except Exception:
            logger.exception("rejection reply failed on channel=%s", message.channel)
        return
    result = ORCHESTRATOR.handle_inbound(message)
    reply = _reply_for(message, result.decision, result.reply)
    if adapter is None:
        logger.warning("no adapter for channel=%s; reply dropped", message.channel)
        return
    try:
        await adapter.send_text(message.sender_id, reply)
    except Exception:
        logger.exception("reply failed on channel=%s", message.channel)
