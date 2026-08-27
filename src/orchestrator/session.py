"""Conversation session store: preserves order context across messages.

The agent-orchestration spec requires the orchestrator to carry context
(customer, items, reservations) between agents and to resume the correct order
when the owner responds later (human-in-the-loop wait). This store keys that
context by the sender id and expires it after a TTL so stale conversations do
not linger; the pipeline keeps state here instead of in module globals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class ResolvedItem:
    """One order line resolved to a catalog SKU, carried between agents."""

    sku: str
    cantidad: int
    description: str | None = None


@dataclass(frozen=True)
class ChatMessage:
    """One conversational turn (role ∈ {"system", "user", "assistant"})."""

    role: str
    content: str


@dataclass
class ConversationState:
    """Context for one sender's order, preserved across pipeline steps."""

    sender_id: str
    customer_id: int | None = None
    order_id: int | None = None
    items: tuple[ResolvedItem, ...] = ()
    awaiting_decision: bool = False
    history: tuple[ChatMessage, ...] = ()  # multi-turn chat log shared by agents
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def with_updates(self, **changes: Any) -> ConversationState:
        """Return a copy with the given fields replaced and the clock touched."""
        changes["updated_at"] = datetime.now(UTC)
        return replace(self, **changes)


class ConversationStore:
    """In-memory TTL-backed store of conversation state keyed by sender id.

    ``ttl`` (default 30 minutes, aligned with the reservation soft-lock) bounds
    how long an abandoned conversation keeps context; ``now`` is injectable for
    tests. Expired entries are dropped lazily on access.
    """

    def __init__(
        self,
        *,
        ttl: timedelta | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.ttl = ttl or timedelta(minutes=30)
        self._now = now or (lambda: datetime.now(UTC))
        self._states: dict[str, ConversationState] = {}

    def get(self, sender_id: str) -> ConversationState | None:
        """Return the sender's state, or ``None`` when absent or expired."""
        state = self._states.get(sender_id)
        if state is None:
            return None
        if self._is_expired(state):
            self._states.pop(sender_id, None)
            return None
        return state

    def put(self, state: ConversationState) -> None:
        """Store (or refresh) a sender's state."""
        self._states[state.sender_id] = state

    def drop(self, sender_id: str) -> None:
        """Remove a sender's state (order finished or abandoned)."""
        self._states.pop(sender_id, None)

    def _is_expired(self, state: ConversationState) -> bool:
        return self._now() - state.updated_at > self.ttl