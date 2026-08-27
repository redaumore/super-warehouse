"""Sourcing case classification: Case A (full stock), B (partial + supplier), C (none).

Pure decision over each item's availability and the supplier searcher:

- every item covered by stock → Case A (PENDING_ASSEMBLY);
- some item's quantity exceeds availability AND every missing item has at least
  one supplier candidate → Case B (IN_PREPARATION);
- some missing item has NO supplier candidate → Case C (CANCELLED).

An item unknown to the catalog/inventory (no inventory row → zero on hand) is
treated as missing and reported — never silently dropped.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass

from src.orchestrator.session import ResolvedItem
from src.supplier.searcher import SupplierCandidate, SupplierCatalogSearcher


class SourcingCase(str, enum.Enum):
    """The three sourcing outcomes of an order."""

    A = "A"
    B = "B"
    C = "C"


@dataclass(frozen=True)
class MissingItem:
    """One item whose requested quantity exceeds the available stock."""

    sku: str
    description: str | None
    requested: int
    missing_quantity: int
    candidates: tuple[SupplierCandidate, ...] = ()


@dataclass(frozen=True)
class SourcingDecision:
    """Classification outcome: the case plus the missing items (B/C)."""

    case: SourcingCase
    missing: tuple[MissingItem, ...] = ()


Availability = Callable[[str], int]


def classify_case(
    items: tuple[ResolvedItem, ...],
    availability: Availability,
    searcher: SupplierCatalogSearcher,
) -> SourcingDecision:
    """Classify a resolved order into Case A/B/C from availability + suppliers."""
    missing: list[MissingItem] = []
    for item in items:
        on_hand = max(0, availability(item.sku))
        if on_hand >= item.cantidad:
            continue
        need = item.cantidad - on_hand
        candidates = searcher.search(sku=item.sku, description=item.description)
        missing.append(
            MissingItem(
                sku=item.sku,
                description=item.description,
                requested=item.cantidad,
                missing_quantity=need,
                candidates=candidates,
            )
        )
    if not missing:
        return SourcingDecision(case=SourcingCase.A)
    if any(not item.candidates for item in missing):
        return SourcingDecision(case=SourcingCase.C, missing=tuple(missing))
    return SourcingDecision(case=SourcingCase.B, missing=tuple(missing))