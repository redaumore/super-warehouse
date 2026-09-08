"""Domain contracts shared across module boundaries (pure types only).

Every symbol here was moved verbatim from its origin module so the type
contract is owned once, in the foundation layer, instead of being reached
across same-layer module edges:

- ``ProductSource``/``ProductEntry`` ← ``src.agents.product_search``
- ``SupplierCandidate``/``SupplierCatalogSearcher`` ← ``src.supplier.searcher``
- ``MissingItem`` ← ``src.sourcing.classify``
- ``ResolvedItem``/``SourcingNeedItem``/``ChatMessage``/``ConversationState``
  ← ``src.orchestrator.session``
- ``AgentName``/``RoutingDecision``/``AgentOutcome`` ← ``src.orchestrator.router``

This module may import from the L0 foundation layer only (``src.db``,
``src.config``, ``src.tz``) — never from L1/L2/L3. Runtime implementations,
I/O stores and parsers stay in their origin modules.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from src.db.models import Cliente


class ProductSource(str, enum.Enum):
    """Where the product-query results came from."""

    LOCAL = "LOCAL"
    RAG = "RAG"
    NONE = "NONE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ProductEntry:
    """One product-query result entry, source-labeled, ready for note rendering."""

    sku: str
    name: str
    source: ProductSource
    provider: str | None = None
    brand: str | None = None
    price: float | None = None
    currency: str | None = None
    unit: str | None = None
    specs: str | None = None
    source_file: str | None = None
    page: int | None = None
    codigo_proveedor: str | None = None


@dataclass(frozen=True)
class SupplierCandidate:
    """One supplier offer for a missing item.

    ``status`` mirrors the supplier master-data lifecycle: candidates MUST
    carry it and searchers MUST exclude INACTIVO suppliers (spec:
    supplier-catalog-search). The default keeps hand-built candidates usable.
    """

    supplier_id: int
    business_name: str
    sku: str
    description: str
    available_quantity: int | None = None
    status: str = "ACTIVO"


@dataclass(frozen=True)
class MissingItem:
    """One item whose requested quantity exceeds the available stock."""

    sku: str
    description: str | None
    requested: int
    missing_quantity: int
    candidates: tuple[SupplierCandidate, ...] = ()


class SupplierCatalogSearcher(Protocol):
    """Query which suppliers can offer a missing item (SKU or free text).

    Seam contract: search results MUST exclude INACTIVO suppliers. The DB-backed
    implementation is out of scope; every real/fake implementation stands in
    behind this protocol.
    """

    def search(
        self,
        *,
        sku: str | None = None,
        description: str | None = None,
    ) -> tuple[SupplierCandidate, ...]:
        """Return candidate suppliers for the missing item, best first."""
        ...


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
