"""Walking-skeleton pipeline: orchestrator + agent stubs + channel reply.

Composes the real ``Orchestrator`` (router + in-memory session store) with thin
stub agent handlers and sends a reply back on the inbound channel, so a message
round-trips end-to-end through routing and persistence. Customer replies come
from the LLM responder (OpenAI, greeting fallback when unconfigured) and each
text turn also queries Postgres for catalog context (``DbCatalogSearcher``),
which degrades gracefully — no catalog note — when the DB is down; the other
five agents stay stubs.

The reply is bridged at the pipeline edge: the sync ``Orchestrator`` returns a
``TurnResult``, and the reply comes from the routed agent when it provides one;
``_reply_for`` composes the skeleton echo (media ack + stage echo) only as a
fallback for the stub agents and sends the text via the async channel adapter
(``Channel.send_text``).
"""

from __future__ import annotations

import logging

from src.agents.customer import (
    CatalogSearcher,
    CustomerResponder,
    DbCatalogSearcher,
    build_handler,
)
from src.channels import CHANNELS
from src.channels.base import InboundMessage
from src.integrations.openai import OpenAIResponder
from src.orchestrator.router import (
    AgentName,
    AgentOutcome,
    Orchestrator,
    RoutingDecision,
)
from src.orchestrator.session import ConversationState, ConversationStore

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


def build_orchestrator(
    responder: CustomerResponder | None = None,
    searcher: CatalogSearcher | None = None,
) -> Orchestrator:
    """Build the app's orchestrator: Customer is wired to the real OpenAI-backed responder (greeting fallback when unconfigured) plus a Postgres-backed catalog searcher for context injection; the other five stay walking-skeleton stubs."""
    orchestrator = Orchestrator(ConversationStore())
    for agent in AgentName:
        orchestrator.register(agent, _stub_agent)
    orchestrator.register(
        AgentName.CUSTOMER,
        build_handler(
            responder or OpenAIResponder(),
            searcher=searcher if searcher is not None else DbCatalogSearcher(),
        ),
    )
    return orchestrator


# The single orchestrator instance the intake dispatches to. Its session store
# is in-memory (a persistent store is a later concern; see docs/architecture.md).
ORCHESTRATOR: Orchestrator = build_orchestrator()


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
