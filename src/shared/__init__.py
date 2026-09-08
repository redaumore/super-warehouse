"""Shared foundation: cross-module contracts.

``src.shared.contracts`` holds the domain's shared PURE types — dataclasses,
enums and protocols that several modules exchange but that must not tie any
module to another module's implementation. The types were moved verbatim from
their origin modules (provenance noted in each docstring) so same-layer modules
import the contract instead of each other:

- ``ProductSource``/``ProductEntry`` from ``src.agents.product_search``;
- ``SupplierCandidate``/``SupplierCatalogSearcher`` from ``src.supplier.searcher``;
- ``ResolvedItem``/``SourcingNeedItem``/``ChatMessage``/``ConversationState``
  from ``src.orchestrator.session``;
- ``AgentName``/``RoutingDecision``/``AgentOutcome`` from ``src.orchestrator.router``.

Layering rule: this package sits in the L0 foundation layer. It may import from
L0 modules (``src.db``, ``src.config``, ``src.tz``, ``src.observability``,
``src.features``) and from nothing above (L1 domain, L2 adapters, L3
interface). Keep it that way: contracts must never reach back into the
modules that consume them.
"""

from src.shared.contracts import (
    AgentName,
    AgentOutcome,
    ChatMessage,
    ConversationState,
    ProductEntry,
    ProductSource,
    ResolvedItem,
    RoutingDecision,
    SourcingNeedItem,
    SupplierCandidate,
    SupplierCatalogSearcher,
)

__all__ = [
    "AgentName",
    "AgentOutcome",
    "ChatMessage",
    "ConversationState",
    "ProductEntry",
    "ProductSource",
    "ResolvedItem",
    "RoutingDecision",
    "SourcingNeedItem",
    "SupplierCandidate",
    "SupplierCatalogSearcher",
]
