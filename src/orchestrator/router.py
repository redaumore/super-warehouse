"""Orchestrator: routes inbound messages and coordinates the agent pipeline.

Implements the agent-orchestration routing contract:

- voice notes and images always go to the Perception agent (STT / vision) —
  a barcode photo is routed to perception, never to the order-intake path;
- a reply that resolves to an owner decision (approve/reject) goes to Dispatch,
  resuming the order that is awaiting the decision (human-in-the-loop wait);
- an in-progress order with resolved items goes back to Sales (adjustments,
  confirmations); one still resolving items goes to Disambiguation;
- a fresh message with no context starts the pipeline at the Customer agent.

``Orchestrator`` wires routing to the store: it loads the sender's context,
routes, hands the message to the registered agent handler, and persists any
updated context so no step loses the order's identity.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from src.channels.base import InboundMessage
from src.orchestrator.session import ConversationState, ConversationStore


class AgentName(str, enum.Enum):
    """The six specialized agents of the pipeline (per the spec)."""

    PERCEPTION = "perception"
    CUSTOMER = "customer"
    DISAMBIGUATION = "disambiguation"
    INVENTORY = "inventory"
    SALES = "sales"
    DISPATCH = "dispatch"


@dataclass(frozen=True)
class RoutingDecision:
    """Where one inbound message goes and what kind of media it carries."""

    agent: AgentName
    media_kind: str | None = None  # "voice" | "image" for the perception agent
    context_loaded: bool = False


class AgentHandler(Protocol):
    """An agent handler: message + current context + routing → updated context."""

    def __call__(
        self,
        message: InboundMessage,
        state: ConversationState | None,
        decision: RoutingDecision,
    ) -> ConversationState | None:
        """Process ``message`` and return the updated conversation state."""


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
        agents: dict[AgentName, Callable[..., ConversationState | None]] | None = None,
    ) -> None:
        self.store = store
        self.agents: dict[AgentName, Callable[..., ConversationState | None]] = agents or {}

    def register(self, agent: AgentName, handler: Callable[..., ConversationState | None]) -> None:
        """Bind an agent handler to its name."""
        self.agents[agent] = handler

    def handle_inbound(self, message: InboundMessage) -> RoutingDecision:
        """Process one inbound message through the routed agent.

        Context is loaded before routing and persisted after the handler, so a
        multi-step order never loses its identity between agents.
        """
        state = self.store.get(message.sender_id)
        decision = route_message(message, state)
        handler = self.agents.get(decision.agent)
        updated = None
        if handler is not None:
            updated = handler(message, state, decision)
        if updated is not None:
            self.store.put(updated)
        return decision