"""Walking-skeleton pipeline: orchestrator + agent stubs + channel reply.

Composes the real ``Orchestrator`` (router + in-memory session store) with thin
stub agent handlers and sends a reply back on the inbound channel, so a message
round-trips end-to-end through routing and persistence without yet needing
OpenAI/Postgres/Sheets. Real agents replace the stubs incrementally.

The reply is bridged at the pipeline edge: the sync ``Orchestrator`` returns a
``RoutingDecision``, and this module composes the reply text and sends it via the
async channel adapter (``Channel.send_text``).
"""

from __future__ import annotations

import logging

from src.channels import CHANNELS
from src.channels.base import InboundMessage
from src.orchestrator.router import AgentName, Orchestrator, RoutingDecision
from src.orchestrator.session import ConversationState, ConversationStore

logger = logging.getLogger(__name__)


def _stub_agent(
    message: InboundMessage,
    state: ConversationState | None,
    _decision: RoutingDecision,
) -> ConversationState:
    """Walking-skeleton agent stub: no domain work — preserve (or seed) context."""
    if state is not None:
        return state.with_updates()
    return ConversationState(sender_id=message.sender_id)


def build_orchestrator() -> Orchestrator:
    """Build the app's orchestrator with all six agents bound to stubs."""
    orchestrator = Orchestrator(ConversationStore())
    for agent in AgentName:
        orchestrator.register(agent, _stub_agent)
    return orchestrator


# The single orchestrator instance the intake dispatches to. Its session store
# is in-memory (a persistent store is a later concern; see docs/architecture.md).
ORCHESTRATOR: Orchestrator = build_orchestrator()


def _reply_for(message: InboundMessage, decision: RoutingDecision) -> str:
    """Compose the walking-skeleton reply for one routing decision."""
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
    decision = ORCHESTRATOR.handle_inbound(message)
    reply = _reply_for(message, decision)
    adapter = CHANNELS.get(message.channel)
    if adapter is None:
        logger.warning("no adapter for channel=%s; reply dropped", message.channel)
        return
    try:
        await adapter.send_text(message.sender_id, reply)
    except Exception:
        logger.exception("reply failed on channel=%s", message.channel)
