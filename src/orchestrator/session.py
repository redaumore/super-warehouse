"""Conversation session store: preserves order context across messages.

The agent-orchestration spec requires the orchestrator to carry context
(customer, items, reservations) between agents and to resume the correct order
when the owner responds later (human-in-the-loop wait). This store keys that
context by the sender id and expires it after a TTL so stale conversations do
not linger; the pipeline keeps state here instead of in module globals.

The sourcing axis extends the state with the Case B supplier-selection turn:
``sourcing_selection_pending`` marks an owner reply that must reach the
SOURCING confirm flow, and ``sourcing_needs``/``sourcing_candidates`` carry the
missing items and the supplier options shown. The owner-pivot axis adds
``customer_disambiguation_pending``/``customer_candidates`` for the numbered
customer-name menu. Because the in-memory store expires after 30 minutes,
``rehydrate_conversation`` rebuilds the OWNER's state from the database (latest
open Order across all customers + its SourcingNeed rows) — the DB is the source
of truth for the multi-turn flows, so they survive the TTL.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.product_search import ProductEntry
from src.db.models import (
    Cliente,
    Order,
    OrderEstado,
    SourcingNeed,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)
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
    po_item_id: int | None = None


@dataclass(frozen=True)
class ChatMessage:
    """One conversational turn (role ∈ {"system", "user", "assistant"})."""

    role: str
    content: str


@dataclass
class ConversationState:
    """Context for one sender's order, preserved across pipeline steps."""

    sender_id: str
    session_id: str | None = None
    customer_id: int | None = None
    order_id: int | None = None
    items: tuple[ResolvedItem, ...] = ()
    awaiting_decision: bool = False
    history: tuple[ChatMessage, ...] = ()  # multi-turn chat log shared by agents
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Sourcing axis (added by the order-sourcing workflow).
    sourcing_selection_pending: bool = False  # awaiting the owner's supplier choice
    sourcing_needs: tuple[SourcingNeedItem, ...] = ()
    sourcing_candidates: tuple[SupplierCandidate, ...] = ()
    # Owner pivot axis: customer-name disambiguation (numbered menu pick).
    customer_disambiguation_pending: bool = False  # awaiting the owner's client pick
    customer_candidates: tuple[Cliente, ...] = ()  # the numbered menu options
    # Product-query axis (rag-product-query change): the last displayed results
    # (referenced by "el 2"-style add intents) and the order-building draft
    # accumulation across queries (local + RAG entries, added by the add-intent
    # short-circuit; draft-only, never persisted to the DB).
    product_options: tuple[ProductEntry, ...] = ()
    draft_items: tuple[tuple[ProductEntry, int], ...] = ()
    # Guided (scripted) order-creation flow: the question the conversation is
    # waiting on ("ask_client" | "ask_product" | "ask_quantity" | "ask_more"),
    # the numbered product options shown for a pick, and the product already
    # chosen that still needs its quantity. Draft-only bookkeeping: never
    # rehydrated from the DB (an expired guided flow just restarts with the
    # session-reset trigger).
    guided_step: str | None = None
    guided_product_options: tuple[ProductEntry, ...] = ()
    guided_product: ProductEntry | None = None

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
    order_ref: int | None = None,
) -> ConversationState | None:
    """Rebuild the owner's state from the DB when the in-memory entry is gone.

    Owner-keyed: the latest DRAFT order ACROSS ALL customers is restored (there
    is no owner entity — the latest open draft IS the owner's, per the design).
    An explicit ``order_ref`` (``pedido #N``) targets that specific order
    instead of the latest. When no DRAFT exists, the latest CONFIRMED order
    with UNASSIGNED sourcing needs is restored instead — the confirm ceremony
    confirms a Case B order while its supplier selection is still pending, and
    that selection turn must survive the TTL. The state carries the order's
    items, its SourcingNeed rows and the routing flags: a Case B order still
    awaiting supplier choices is restored with ``sourcing_selection_pending``
    and the candidates recomputed through the searcher; a Draft awaiting
    confirm restores ``awaiting_decision`` so the owner's confirm/cancel reply
    routes correctly. CANCELED orders never rehydrate.
    """
    if order_ref is not None:
        order = session.get(Order, order_ref)
        if order is None or order.estado is OrderEstado.CANCELED:
            return None
    else:
        order = session.scalar(
            select(Order)
            .where(Order.estado == OrderEstado.DRAFT)
            .order_by(Order.order_id.desc())
            .limit(1)
        )
        if order is None:
            order = session.scalar(
                select(Order)
                .where(
                    Order.estado == OrderEstado.CONFIRMED,
                    Order.order_id.in_(
                        select(SourcingNeed.order_id).where(SourcingNeed.supplier_id.is_(None))
                    ),
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
            po_item_id=need.po_item_id,
        )
        for need in session.scalars(
            select(SourcingNeed).where(SourcingNeed.order_id == order.order_id)
        )
    )
    # The selection turn stays pending while any need is unassigned OR any of
    # the order's purchase orders is still OPEN (re-selection before execution).
    linked_items = [need.po_item_id for need in needs if need.po_item_id is not None]
    open_po_exists = False
    if linked_items:
        open_po_exists = (
            session.scalar(
                select(SupplierPurchaseOrderItem.po_id)
                .join(
                    SupplierPurchaseOrder,
                    SupplierPurchaseOrder.po_id == SupplierPurchaseOrderItem.po_id,
                )
                .where(
                    SupplierPurchaseOrderItem.po_item_id.in_(linked_items),
                    SupplierPurchaseOrder.estado == SupplierPurchaseOrderState.OPEN,
                )
                .limit(1)
            )
            is not None
        )
    selection_pending = bool(needs) and (
        any(need.supplier_id is None for need in needs) or open_po_exists
    )
    # A Draft awaiting the owner's confirm (or cancel) owns the next reply.
    awaiting_decision = order.estado is OrderEstado.DRAFT and not selection_pending
    candidates: tuple[SupplierCandidate, ...] = ()
    if selection_pending and searcher is not None:
        collected: list[SupplierCandidate] = []
        for need in needs:
            if need.supplier_id is None:
                collected.extend(searcher.search(sku=need.sku))
        candidates = tuple(collected)
    return ConversationState(
        sender_id=sender_id,
        customer_id=order.customer_id,
        order_id=order.order_id,
        items=items,
        awaiting_decision=awaiting_decision,
        sourcing_selection_pending=selection_pending,
        sourcing_needs=needs,
        sourcing_candidates=candidates,
    )
