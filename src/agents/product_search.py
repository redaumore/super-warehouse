"""Product query resolution: local-first → RAG-fallback precedence chain.

The conversational product query is resolved by a ``PrecedenceProductSearcher``
that runs the local catalog search first and only calls the supplier-catalog
RAG when the local hop returns zero candidates at/above the ambiguity floor.
A local hit never reaches the RAG; an empty local search falls back to the RAG
with the same free-form text; a local ``SQLAlchemyError`` still falls back to
the RAG (the chain never raises on either leg — failures surface as the
``ERROR`` source instead). The result carries a source discriminator
(``LOCAL | RAG | NONE | ERROR``) so the Customer agent can render an honest,
source-aware note.

``parse_product_add`` implements the order-building intent parser: natural
phrases such as "agregalo" or "sumá 5 de eso" resolve to the last displayed
product (index 0) with the given quantity, and "el 2" resolves to the second
displayed result — both bounded by the currently displayed ``options``.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from src.agents.disambiguation import SearchCandidate, normalize_text
from src.integrations.rag import RagProduct, RagProductClient, RagProductError

logger = logging.getLogger(__name__)


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
class ProductSearchResult:
    """Outcome of one product query: source + the entries to display."""

    source: ProductSource
    entries: tuple[ProductEntry, ...] = ()


class LocalSearcher(Protocol):
    """Local catalog search boundary (structurally satisfied by DbCatalogSearcher)."""

    def search(self, query: str) -> tuple[SearchCandidate, ...]:
        """Return catalog candidates for ``query``, best first."""


class ProductSearcher(Protocol):
    """Product-query boundary the Customer agent talks through."""

    def search(self, query: str) -> ProductSearchResult:
        """Resolve ``query`` and return a source-discriminated result."""


class PrecedenceProductSearcher:
    """Local-first → RAG-fallback chain behind the ``ProductSearcher`` seam.

    A "hit" is one or more local candidates at/above ``floor`` (the ambiguity
    floor); anything else — empty local results, or a local ``SQLAlchemyError``
    — falls back to the RAG. RAG failures (timeout, transport, HTTP error)
    never raise: they become ``ProductSearchResult(ERROR)`` so the caller can
    tell the owner the catalogs could not be consulted instead of claiming the
    item is out of stock.
    """

    def __init__(
        self,
        local: LocalSearcher,
        client: RagProductClient,
        *,
        floor: float = 0.65,
    ) -> None:
        self.local = local
        self.client = client
        self.floor = floor

    def search(self, query: str) -> ProductSearchResult:
        """Resolve ``query``: local hit → LOCAL; empty/failed local → RAG fallback."""
        try:
            candidates = self.local.search(query)
        except SQLAlchemyError:
            logger.warning("local catalog search failed for query=%r; falling back to RAG", query)
            return self._rag_fallback(query)
        qualified = [c for c in candidates if c.confidence >= self.floor]
        if qualified:
            entries = tuple(
                ProductEntry(
                    sku=candidate.sku,
                    name=candidate.nombre_oficial,
                    source=ProductSource.LOCAL,
                )
                for candidate in qualified
            )
            return ProductSearchResult(source=ProductSource.LOCAL, entries=entries)
        return self._rag_fallback(query)

    def _rag_fallback(self, query: str) -> ProductSearchResult:
        try:
            products = self.client.query(query)
        except RagProductError as exc:
            logger.warning("rag query failed for query=%r: %s", query, exc)
            return ProductSearchResult(source=ProductSource.ERROR)
        if not products:
            return ProductSearchResult(source=ProductSource.NONE)
        entries = tuple(_entry_from_rag(product) for product in products)
        return ProductSearchResult(source=ProductSource.RAG, entries=entries)


def _entry_from_rag(product: RagProduct) -> ProductEntry:
    """Map a typed RAG product into a source-labeled product entry."""
    return ProductEntry(
        sku=product.sku,
        name=product.name,
        source=ProductSource.RAG,
        provider=product.provider,
        brand=product.brand,
        price=product.price,
        currency=product.currency,
        unit=product.unit,
            specs=product.specs,
            source_file=product.source_file,
            page=product.page,
            codigo_proveedor=product.codigo_proveedor,
        )


_NUMBERED_REF_RE = re.compile(r"\bel\s+(\d+)\b")
_QUANTITY_ADD_RE = re.compile(r"\b(?:suma|agrega)(?:le|les|los|las)?\s+(\d+)\s+de\s+eso\w*")
_DIRECT_ADD_RE = re.compile(r"\b(?:agregalo|agregala|agregalos|agregalas|sumalo|sumala)\b")
_FINALIZE_RE = re.compile(
    r"^\s*(?:cerr(?:a|á)|cierra|cerrar|finaliz(?:a|á)|finaliza|finalizar|"
    r"termin(?:a|á)|termina|terminar|confirm(?:a|á)|confirma|confirmar)\s+"
    r"(?:el\s+)?(?:pedido|orden)(?:\s*(?::|para|de)\s*(.+?))?\s*[.!?]*$",
    re.IGNORECASE,
)


def parse_product_add(text: str, options: Sequence[ProductEntry]) -> tuple[int, int] | None:
    """Parse an add-to-order intent; ``None`` when the text is not an add phrase.

    Returns ``(index, quantity)`` where ``index`` is bounded by ``options``:
    "el 2" picks the second displayed result, "sumá 5 de eso" adds 5 of the
    last displayed product, "agregalo" adds 1 of it. An out-of-range numbered
    reference or an empty option list yields ``None``.
    """
    if not options:
        return None
    norm = normalize_text(text)
    numbered = _NUMBERED_REF_RE.search(norm)
    if numbered is not None:
        index = int(numbered.group(1)) - 1
        if 0 <= index < len(options):
            return (index, 1)
        return None
    quantity = _QUANTITY_ADD_RE.search(norm)
    if quantity is not None:
        return (0, int(quantity.group(1)))
    if _DIRECT_ADD_RE.search(norm) is not None:
        return (0, 1)
    return None


def is_finalize(text: str) -> bool:
    """Return whether ``text`` is a finalize command, with or without a name."""
    return _FINALIZE_RE.match(text or "") is not None


def parse_finalize(
    text: str, draft_items: Sequence[tuple[ProductEntry, int]]
) -> str | None:
    """Extract the customer name from a finalize command for a non-empty draft.

    The parser deliberately requires draft lines so ordinary requests such as
    ``finalizar el pedido`` cannot create an empty order. A command without a
    customer name is still recognized by :func:`is_finalize`; the customer
    handler can then use an already attached session customer or ask for one.
    """
    if not draft_items:
        return None
    match = _FINALIZE_RE.match(text or "")
    if match is None or match.group(1) is None:
        return None
    name = " ".join(match.group(1).strip(" .!? ").split())
    return name or None
