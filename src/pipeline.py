"""Walking-skeleton pipeline: orchestrator + agent stubs + channel reply.

Composes the real ``Orchestrator`` (router + in-memory session store) with thin
stub agent handlers and sends a reply back on the inbound channel, so a message
round-trips end-to-end through routing and persistence. Customer replies come
from the LLM responder (OpenAI, greeting fallback when unconfigured) and each
text turn also queries Postgres for catalog context (``DbCatalogSearcher``),
which degrades gracefully — no catalog note — when the DB is down; the other
agents stay stubs.

When the owner phone is configured, the order-sourcing workflow is enabled:
the parse step extracts structured order fields before the Customer agent, the
Customer handler classifies each order into Case A/B/C, and the SOURCING agent
handles the owner's supplier-selection replies. The conversation store is wired
to rehydrate expired conversations from the database, so a multi-turn Case B
selection survives the 30-minute in-memory TTL. Clearing ``OWNER_PHONE`` turns
the parse step off and keeps the legacy conversational intake.

The reply is bridged at the pipeline edge: the sync ``Orchestrator`` returns a
``TurnResult``, and the reply comes from the routed agent when it provides one;
``_reply_for`` composes the skeleton echo (media ack + stage echo) only as a
fallback for the stub agents and sends the text via the async channel adapter
(``Channel.send_text``).
"""

from __future__ import annotations

import asyncio
import logging

from src.agents.customer import (
    CatalogSearcher,
    CustomerResponder,
    DbCatalogSearcher,
    SourcingDeps,
    build_handler,
)
from src.agents.intake import OrderParser, SimpleOrderParser
from src.channels import CHANNELS
from src.channels.base import Channel, InboundMessage
from src.config import get_settings
from src.db.session import SessionLocal
from src.integrations.openai import OpenAIResponder
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


class _ChannelNotifier:
    """Bridges the sync Notifier protocol to an async channel adapter.

    The orchestrator runs inside the async intake handler, so a running event
    loop exists when notifications are sent; each send is scheduled as a
    background task on that loop. Without a loop the notification is dropped
    with a warning (never raises).
    """

    def __init__(self, channel: Channel) -> None:
        """Wrap one channel adapter as a sync Notifier bridge."""
        self._channel = channel

    def send_text(self, recipient: str, text: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("notification dropped (no event loop): %s", text[:80])
            return
        loop.create_task(self._channel.send_text(recipient, text))


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
    """
    settings = get_settings()
    if not settings.owner_phone:
        return None
    from src.channels import CHANNELS

    return SourcingDeps(
        session_factory=SessionLocal,
        searcher=FakeSupplierCatalogSearcher(),
        notifier=_ChannelNotifier(CHANNELS["telegram"]),
        owner_phone=settings.owner_phone,
    )


def build_orchestrator(
    responder: CustomerResponder | None = None,
    searcher: CatalogSearcher | None = None,
    *,
    sourcing: SourcingDeps | None = None,
    parser: OrderParser | None = None,
) -> Orchestrator:
    """Build the app's orchestrator.

    Customer is wired to the real OpenAI-backed responder (greeting fallback
    when unconfigured) plus a Postgres-backed catalog searcher; the other
    agents stay walking-skeleton stubs. With ``sourcing`` wired, the parse
    step and the SOURCING confirm agent are enabled and the store rehydrates
    expired conversations from the database.
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
            searcher=searcher if searcher is not None else DbCatalogSearcher(),
            sourcing=sourcing,
        ),
    )
    if sourcing is not None:
        orchestrator.register(AgentName.SOURCING, build_sourcing_handler(SessionLocal))
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
    """Route one inbound message through the orchestrator and reply on its channel."""
    result = ORCHESTRATOR.handle_inbound(message)
    reply = _reply_for(message, result.decision, result.reply)
    adapter = CHANNELS.get(message.channel)
    if adapter is None:
        logger.warning("no adapter for channel=%s; reply dropped", message.channel)
        return
    try:
        await adapter.send_text(message.sender_id, reply)
    except Exception:
        logger.exception("reply failed on channel=%s", message.channel)