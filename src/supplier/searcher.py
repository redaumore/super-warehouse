"""Supplier catalog searcher seam.

Exposes the ``SupplierCatalogSearcher`` protocol that the sourcing workflow
consumes to learn which suppliers can offer a missing item — without coupling
the workflow to the external supplier-catalog RAG implementation (which is not
built yet). ``FakeSupplierCatalogSearcher`` is the in-memory stand-in used by
tests and demos.

The pure contract types (``SupplierCandidate``, ``SupplierCatalogSearcher``)
live in ``src.shared.contracts``; this module re-exports them for backwards
compatibility and keeps the runtime implementations (the in-memory fake and
the DB-backed RAG searcher in ``rag_searcher``).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from src.shared.contracts import SupplierCandidate, SupplierCatalogSearcher

__all__ = [
    "FakeSupplierCatalogSearcher",
    "SupplierCandidate",
    "SupplierCatalogSearcher",
]


def _fold(text: str) -> str:
    """Lowercase and strip accents for substring matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().strip()


class FakeSupplierCatalogSearcher:
    """In-memory searcher: returns candidates matching the SKU or description.

    A candidate matches when its ``sku`` equals the requested SKU, or — when no
    SKU is given — when the folded description needle appears in the
    candidate's folded description or SKU. INACTIVO candidates are excluded
    (seam contract), mirroring what the DB-backed searcher must do. No external
    RAG is involved.
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
            if candidate.status == "INACTIVO":
                continue
            sku_hit = sku is not None and candidate.sku == sku
            desc_hit = needle is not None and (
                needle in _fold(candidate.description) or needle in _fold(candidate.sku)
            )
            if sku_hit or desc_hit:
                matches.append(candidate)
        return tuple(matches)
