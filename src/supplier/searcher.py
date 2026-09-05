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
