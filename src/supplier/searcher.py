"""Supplier catalog searcher seam.

Exposes the ``SupplierCatalogSearcher`` protocol that the sourcing workflow
consumes to learn which suppliers can offer a missing item — without coupling
the workflow to the external supplier-catalog RAG implementation (which is not
built yet). ``FakeSupplierCatalogSearcher`` is the in-memory stand-in used by
tests and demos.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SupplierCandidate:
    """One supplier offer for a missing item."""

    supplier_id: int
    business_name: str
    sku: str
    description: str
    available_quantity: int | None = None


class SupplierCatalogSearcher(Protocol):
    """Query which suppliers can offer a missing item (SKU or free text)."""

    def search(
        self,
        *,
        sku: str | None = None,
        description: str | None = None,
    ) -> tuple[SupplierCandidate, ...]:
        """Return candidate suppliers for the missing item, best first."""


def _fold(text: str) -> str:
    """Lowercase and strip accents for substring matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().strip()


class FakeSupplierCatalogSearcher:
    """In-memory searcher: returns candidates matching the SKU or description.

    A candidate matches when its ``sku`` equals the requested SKU, or — when no
    SKU is given — when the folded description needle appears in the
    candidate's folded description or SKU. No external RAG is involved.
    """

    def __init__(self, candidates: Sequence[SupplierCandidate] = ()) -> None:
        self.candidates = tuple(candidates)

    def search(
        self,
        *,
        sku: str | None = None,
        description: str | None = None,
    ) -> tuple[SupplierCandidate, ...]:
        needle = _fold(description) if description else None
        matches: list[SupplierCandidate] = []
        for candidate in self.candidates:
            if sku is not None and candidate.sku == sku:
                matches.append(candidate)
            elif (
                sku is None
                and needle is not None
                and (
                    needle in _fold(candidate.description)
                    or needle in _fold(candidate.sku)
                )
            ):
                matches.append(candidate)
        return tuple(matches)