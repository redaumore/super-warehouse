"""Conversation session store: preserves order context across messages.

The agent-orchestration spec requires the orchestrator to carry context
(customer, items, reservations) between agents and to resume the correct order
when the owner responds later (human-in-the-loop wait). This store keys that
context by the sender id and expires it after a TTL so stale conversations do
not linger; the pipeline keeps state here instead of in module globals.

The sourcing axis extends the state with the Case B supplier-selection turn:
``sourcing_selection_pending`` marks an owner reply that must reach the
SOURCING confirm flow, and ``sourcing_needs``/``sourcing_candidates`` carry the
missing items and the supplier options shown. Because the in-memory store
expires after 30 minutes, ``rehydrate_conversation`` rebuilds a sender's state
from the database (latest open Order + its SourcingNeed rows) — the DB is the
source of truth for the multi-turn selection, so it survives the TTL.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.intake import ParsedOrder
from src.db.models import Cliente, Order, OrderEstado, SourcingNeed, SourcingState
from src.supplier.searcher import SupplierCandidate, SupplierCatalogSearcher


@dataclass(frozen=True)
class ResolvedItem:
    """One order line resolved to a catalog SKU, carried between agents."""

    sku: str
    cantidad: int
    description: str | None = None


@dataclass(frozen=True)
class SourcingNeedItem:
    """One missing item of a Case B order, recoverable from the DB."""

    sku: str
    missing_quantity: int
    supplier_id: int | None = None
    need_id: int | None = None


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
    # Sourcing axis (added by the order-sourcing workflow).
    parsed_order: ParsedOrder | None = None  # parse-step output for the current turn
    sourcing_selection_pending: bool = False  # awaiting the owner's supplier choice
    sourcing_needs: tuple[SourcingNeedItem, ...] = ()
    sourcing_candidates: tuple[SupplierCandidate, ...] = ()

    def with_updates(self, **changes: Any) -> ConversationState:
        """Return a copy with the given fields replaced and the clock touched."""
        changes["updated_at"] = datetime.now(UTC)
        return replace(self, **changes)


class ConversationStore:
    """In-memory TTL-backed store of conversation state keyed by sender id.

    ``ttl`` (default 30 minutes, aligned with the reservation soft-lock) bounds
    how long an abandoned conversation keeps context; ``now`` is injectable for
    tests. Expired entries are dropped lazily on access; when a ``rehydrator``
    is wired, a miss (absent or expired) falls back to rebuilding the state
    from the database, so the multi-turn sourcing selection survives the TTL.
    """

    def __init__(
        self,
        *,
        ttl: timedelta | None = None,
        now: Callable[[], datetime] | None = None,
        rehydrator: Callable[[str], ConversationState | None] | None = None,
    ) -> None:
        self.ttl = ttl or timedelta(minutes=30)
        self._now = now or (lambda: datetime.now(UTC))
        self.rehydrator = rehydrator
        self._states: dict[str, ConversationState] = {}

    def get(self, sender_id: str) -> ConversationState | None:
        """Return the sender's state, or ``None`` when absent or expired.

        A miss is forwarded to the rehydrator (when wired) so a TTL-expired
        Case B selection is rebuilt from the database instead of lost.
        """
        state = self._states.get(sender_id)
        if state is not None and not self._is_expired(state):
            return state
        if state is not None:
            self._states.pop(sender_id, None)
        return self._rehydrate(sender_id)

    def put(self, state: ConversationState) -> None:
        """Store (or refresh) a sender's state."""
        self._states[state.sender_id] = state

    def drop(self, sender_id: str) -> None:
        """Remove a sender's state (order finished or abandoned)."""
        self._states.pop(sender_id, None)

    def _rehydrate(self, sender_id: str) -> ConversationState | None:
        if self.rehydrator is None:
            return None
        state = self.rehydrator(sender_id)
        if state is not None:
            self._states[sender_id] = state
        return state

    def _is_expired(self, state: ConversationState) -> bool:
        return self._now() - state.updated_at > self.ttl


def rehydrate_conversation(
    session: Session,
    sender_id: str,
    *,
    searcher: SupplierCatalogSearcher | None = None,
) -> ConversationState | None:
    """Rebuild a sender's state from the DB when the in-memory entry is gone.

    Uses the sender's latest non-rejected Order plus its SourcingNeed rows.
    A Case B order still awaiting supplier choices is restored with
    ``sourcing_selection_pending`` and the supplier candidates recomputed
    through the searcher; a Case A order awaiting approval restores
    ``awaiting_decision`` so the owner's approve/reject reply routes correctly.
    """
    customer = session.scalar(select(Cliente).where(Cliente.telefono_norm == sender_id))
    if customer is None:
        return None
    order = session.scalar(
        select(Order)
        .where(
            Order.customer_id == customer.customer_id,
            Order.estado != OrderEstado.REJECTED,
        )
        .order_by(Order.order_id.desc())
        .limit(1)
    )
    if order is None:
        return None
    items = tuple(ResolvedItem(sku=item.sku, cantidad=item.cantidad) for item in order.items)
    needs = tuple(
        SourcingNeedItem(
            sku=need.sku,
            missing_quantity=need.missing_quantity,
            supplier_id=need.supplier_id,
            need_id=need.need_id,
        )
        for need in session.scalars(
            select(SourcingNeed).where(SourcingNeed.order_id == order.order_id)
        )
    )
    awaiting_decision = (
        order.estado is OrderEstado.PENDING_APPROVAL
        and order.sourcing_state is SourcingState.PENDING_ASSEMBLY
    )
    selection_pending = (
        order.sourcing_state is SourcingState.IN_PREPARATION
        and any(need.supplier_id is None for need in needs)
    )
    candidates: tuple[SupplierCandidate, ...] = ()
    if selection_pending and searcher is not None:
        collected: list[SupplierCandidate] = []
        for need in needs:
            if need.supplier_id is None:
                collected.extend(searcher.search(sku=need.sku))
        candidates = tuple(collected)
    return ConversationState(
        sender_id=sender_id,
        customer_id=customer.customer_id,
        order_id=order.order_id,
        items=items,
        awaiting_decision=awaiting_decision,
        sourcing_selection_pending=selection_pending,
        sourcing_needs=needs,
        sourcing_candidates=candidates,
    )