"""Orchestrator: routes inbound messages and coordinates the agent pipeline.

Implements the agent-orchestration routing contract:

- voice notes and images always go to the Perception agent (STT / vision) —
  a barcode photo is routed to perception, never to the order-intake path;
- a reply that resolves to an owner decision (approve/reject) goes to Dispatch,
  resuming the order that is awaiting the decision (human-in-the-loop wait);
- a reply on a Case B order that is awaiting the owner's supplier selection
  goes to the SOURCING confirm flow;
- an in-progress order with resolved items goes back to Sales (adjustments,
  confirmations); one still resolving items goes to Disambiguation;
- a fresh message with no context starts the pipeline at the Customer agent —
  after a parse step that extracts structured order fields when a parser is
  wired (the parse step is the intake seam; disabling it keeps legacy routing).

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

from src.agents.intake import OrderParser
from src.channels.base import InboundMessage
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


@dataclass(frozen=True)
class RoutingDecision:
    """Where one inbound message goes and what kind of media it carries."""

    agent: AgentName
    media_kind: str | None = None  # "voice" | "image" for the perception agent
    context_loaded: bool = False
    parsed: bool = False  # True when the parse step extracted an order from the text


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


@dataclass(frozen=True)
class TurnResult:
    """One orchestrator turn — where the message went and the agent's optional reply."""

    decision: RoutingDecision
    reply: str | None = None


def route_message(
    message: InboundMessage, state: ConversationState | None
) -> RoutingDecision:
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
    if state is not None and state.awaiting_decision:
        # The owner conversation owns every reply while a decision is pending:
        # Dispatch parses approve/reject and asks for clarification otherwise.
        return RoutingDecision(agent=AgentName.DISPATCH, context_loaded=True)
    if state is not None and state.sourcing_selection_pending:
        # The owner is mid Case B supplier selection: the reply confirms the
        # chosen suppliers and accumulates the purchase order(s).
        return RoutingDecision(agent=AgentName.SOURCING, context_loaded=True)
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
        *,
        parser: OrderParser | None = None,
    ) -> None:
        self.store = store
        self.agents: dict[AgentName, Callable[..., AgentOutcome | None]] = agents or {}
        self.parser = parser

    def register(
        self, agent: AgentName, handler: Callable[..., AgentOutcome | None]
    ) -> None:
        """Bind an agent handler to its name."""
        self.agents[agent] = handler

    def handle_inbound(self, message: InboundMessage) -> TurnResult:
        """Process one inbound message through the routed agent.

        Context is loaded (rehydrated from the DB when expired) before routing
        and persisted after the handler, so a multi-step order never loses its
        identity between agents. When a parser is wired and the message would
        start a fresh Customer conversation, the parse step runs first: the
        extracted order rides the state so the Customer agent can classify it
        into Case A/B/C instead of answering as plain chat. The agent's
        optional reply rides the turn result back to the pipeline.
        """
        state = self.store.get(message.sender_id)
        decision = route_message(message, state)
        if (
            self.parser is not None
            and decision.agent is AgentName.CUSTOMER
            and not decision.context_loaded
            and message.media_type is None
        ):
            parsed = self.parser.parse((message.text or "").strip())
            if parsed is not None:
                state = ConversationState(sender_id=message.sender_id, parsed_order=parsed)
                decision = RoutingDecision(agent=AgentName.CUSTOMER, parsed=True)
        handler = self.agents.get(decision.agent)
        outcome = None
        if handler is not None:
            outcome = handler(message, state, decision)
        if outcome is not None and outcome.state is not None:
            self.store.put(outcome.state)
        return TurnResult(decision=decision, reply=outcome.reply if outcome else None)