"""Orchestrator: routes inbound messages and coordinates the agent pipeline.

Implements the agent-orchestration routing contract:

- voice notes and images always go to the Perception agent (STT / vision) —
  a barcode photo is routed to perception, never to the order-intake path;
- a reply that resolves to an owner decision (approve/reject) goes to Dispatch,
  resuming the order that is awaiting the decision (human-in-the-loop wait);
- a reply on a Case B order that is awaiting the owner's supplier selection
  goes to the SOURCING confirm flow;
- a conversation seeded by the session reset follows the scripted
  order-creation flow (GUIDED) — client → products → quantity → confirm —
  until the draft is finalized and handed to Dispatch;
- an in-progress order with resolved items goes back to Sales (adjustments,
  confirmations); one still resolving items goes to Disambiguation;
- a fresh message with no context starts the pipeline at the Customer agent —
  the scripted GUIDED flow (session reset) is the only order-creation path.

``Orchestrator`` wires routing to the store: it loads the sender's context
(rehydrating from the DB when the in-memory entry expired), routes, hands the
message to the registered agent handler, and persists any updated context so no
step loses the order's identity.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from src.agents.commands import GUIDED_ASK_CLIENT, is_session_reset
from src.agents.product_search import parse_product_remove
from src.channels.base import InboundMessage
from src.observability.session_logger import generate_session_id, get_current_session_id
from src.orchestrator.session import ConversationState, ConversationStore


class AgentName(str, enum.Enum):
    """The specialized agents of the pipeline (per the spec)."""

    PERCEPTION = "perception"
    CUSTOMER = "customer"
    DISAMBIGUATION = "disambiguation"
    INVENTORY = "inventory"
    SALES = "sales"
    DISPATCH = "dispatch"
    SOURCING = "sourcing"
    GUIDED = "guided"


@dataclass(frozen=True)
class RoutingDecision:
    """Where one inbound message goes and what kind of media it carries."""

    agent: AgentName
    media_kind: str | None = None  # "voice" | "image" for the perception agent
    context_loaded: bool = False


@dataclass(frozen=True)
class AgentOutcome:
    """Result of one agent turn; a handler may omit the reply (pipeline falls back to its skeleton echo)."""

    state: ConversationState | None = None
    reply: str | None = None


class AgentHandler(Protocol):
    """An agent handler: message + current context + routing → turn outcome (state, optional reply)."""

    def __call__(
        self,
        message: InboundMessage,
        state: ConversationState | None,
        decision: RoutingDecision,
    ) -> AgentOutcome | None:
        """Process ``message`` and return the updated state and optional reply."""
        ...


@dataclass(frozen=True)
class TurnResult:
    """One orchestrator turn — where the message went and the agent's optional reply."""

    decision: RoutingDecision
    reply: str | None = None
    state: ConversationState | None = None


def route_message(message: InboundMessage, state: ConversationState | None) -> RoutingDecision:
    """Decide which agent handles an inbound message."""
    if message.media_type == "voice":
        return RoutingDecision(
            agent=AgentName.PERCEPTION, media_kind="voice", context_loaded=state is not None
        )
    if message.media_type == "image":
        return RoutingDecision(
            agent=AgentName.PERCEPTION, media_kind="image", context_loaded=state is not None
        )
    text = (message.text or "").strip()
    if not text:
        # Nothing to route on: text-less, media-less message.
        return RoutingDecision(agent=AgentName.CUSTOMER, context_loaded=state is not None)
    if parse_product_remove(text) is not None:
        # The remove-product command belongs to the Customer agent whenever an
        # order/draft context exists (in-memory draft or rehydrated DRAFT).
        return RoutingDecision(agent=AgentName.CUSTOMER, context_loaded=state is not None)
    if state is not None and state.guided_step is not None:
        # The scripted order-creation flow owns every text turn between the
        # session reset and the draft finalization: the system asks, the
        # owner answers, and no free-form agent runs in between.
        return RoutingDecision(agent=AgentName.GUIDED, context_loaded=True)
    if state is not None and state.awaiting_decision:
        # The owner conversation owns every reply while a decision is pending:
        # Dispatch parses approve/reject and asks for clarification otherwise.
        return RoutingDecision(agent=AgentName.DISPATCH, context_loaded=True)
    if state is not None and state.sourcing_selection_pending:
        # The owner is mid Case B supplier selection: the reply confirms the
        # chosen suppliers and accumulates the purchase order(s).
        return RoutingDecision(agent=AgentName.SOURCING, context_loaded=True)
    if state is not None and state.draft_items:
        # The product-query draft owns the next text turn until it is finalized,
        # even though no persisted order exists yet.
        return RoutingDecision(agent=AgentName.CUSTOMER, context_loaded=True)
    if state is not None and state.order_id is not None:
        if state.items:
            return RoutingDecision(agent=AgentName.SALES, context_loaded=True)
        return RoutingDecision(agent=AgentName.DISAMBIGUATION, context_loaded=True)
    return RoutingDecision(agent=AgentName.CUSTOMER, context_loaded=state is not None)


class Orchestrator:
    """Coordinates the pipeline: load context → route → agent → persist context."""

    def __init__(
        self,
        store: ConversationStore,
        agents: dict[AgentName, Callable[..., AgentOutcome | None]] | None = None,
    ) -> None:
        self.store = store
        self.agents: dict[AgentName, Callable[..., AgentOutcome | None]] = agents or {}

    def register(self, agent: AgentName, handler: Callable[..., AgentOutcome | None]) -> None:
        """Bind an agent handler to its name."""
        self.agents[agent] = handler

    def handle_inbound(self, message: InboundMessage) -> TurnResult:
        """Process one inbound message through the routed agent.

        Context is loaded (rehydrated from the DB when expired) before routing
        and persisted after the handler, so a multi-step order never loses its
        identity between agents. The agent's optional reply rides the turn
        result back to the pipeline.

        The session-reset trigger ("hola bob", case-insensitive, whole
        message, optional trailing punctuation) is checked first: it drops
        the sender's in-memory conversation state — drafts, displayed
        products, pending menus and awaiting decisions — seeds a fresh
        conversation with the guided flow's first step (``guided_step=
        "ask_client"``), and answers with the scripted first question
        ("¿Para qué cliente querés armar un pedido?"). It works from ANY
        state and never touches persisted DB orders. Media messages (voice,
        image) never trigger the reset.
        """
        if message.media_type is None and is_session_reset((message.text or "").strip()):
            self.store.drop(message.sender_id)
            sid = get_current_session_id() or generate_session_id(message.sender_id)
            fresh_state = ConversationState(
                sender_id=message.sender_id, session_id=sid, guided_step="ask_client"
            )
            self.store.put(fresh_state)
            return TurnResult(
                decision=RoutingDecision(agent=AgentName.CUSTOMER),
                reply=GUIDED_ASK_CLIENT,
                state=fresh_state,
            )
        state = self.store.get(message.sender_id)
        if state is not None and state.session_id is None:
            sid = get_current_session_id() or generate_session_id(message.sender_id)
            state = state.with_updates(session_id=sid)
            self.store.put(state)
        decision = route_message(message, state)
        handler = self.agents.get(decision.agent)
        outcome = None
        if handler is not None:
            outcome = handler(message, state, decision)
        final_state = state
        if outcome is not None and outcome.state is not None:
            final_state = outcome.state
            if final_state.session_id is None:
                sid = (
                    state.session_id
                    if state and state.session_id
                    else (get_current_session_id() or generate_session_id(message.sender_id))
                )
                final_state = final_state.with_updates(session_id=sid)
            self.store.put(final_state)
        elif final_state is not None:
            self.store.put(final_state)
        return TurnResult(
            decision=decision,
            reply=outcome.reply if outcome else None,
            state=final_state,
        )
