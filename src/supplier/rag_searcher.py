"""RAG-backed supplier catalog searcher (implements the searcher seam).

Maps the supplier-catalog RAG hits (``RagProductClient.query``) into real
``SupplierCandidate`` objects: each RAG product carries the supplier's 3-char
``codigo_proveedor``, which is resolved against the ``suppliers`` master table.
Candidates whose provider has no supplier row — or whose supplier is
INACTIVO — are dropped, honoring the seam contract that search results MUST
exclude INACTIVO suppliers.

When the RAG is unreachable the searcher degrades to an empty result (logged
warning): missing items then classify as Case C — the documented safe
behavior — instead of raising. The RAG carries no availability data, so
candidates never claim stock (``available_quantity`` is always ``None``).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Supplier, SupplierStatus
from src.integrations.rag import RagProductError
from src.supplier.searcher import SupplierCandidate

logger = logging.getLogger(__name__)


class RagSupplierCatalogSearcher:
    """Search supplier offers via the RAG, mapped to real ``suppliers`` rows."""

    def __init__(self, session_factory: type[Session], rag_client: object) -> None:
        self.session_factory = session_factory
        self.rag_client = rag_client
        # Diagnostic for the last search() call: the RAG provider codes that
        # were dropped because no ACTIVO supplier row matched (unknown code or
        # INACTIVO supplier). Last-call state only — reset on every search();
        # there is a single asyncio loop, so this is NOT concurrency-safe.
        self.last_unmapped_codes: tuple[str, ...] = ()

    def search(
        self,
        *,
        sku: str | None = None,
        description: str | None = None,
    ) -> tuple[SupplierCandidate, ...]:
        """Return mapped supplier candidates for the missing item, best first.

        The free-text RAG query is the description when present, else the SKU
        (both stripped); no text means no query. Failures degrade to an empty
        tuple so the workflow falls back to Case C. RAG hits whose provider has
        no ACTIVO supplier row are dropped; the dropped 3-char codes are left
        on ``last_unmapped_codes`` so callers can notify the owner that the
        supplier master is missing entries (the ingesta must map lists to
        suppliers).
        """
        self.last_unmapped_codes = ()
        text = (description or sku or "").strip()
        if not text:
            return ()
        try:
            products = self.rag_client.query(text)  # type: ignore[attr-defined]
        except RagProductError as exc:
            logger.warning("rag supplier search degraded for %r: %s", text, exc)
            return ()
        with self.session_factory() as session:
            return self._map(session, products)

    def _map(
        self, session: Session, products: tuple
    ) -> tuple[SupplierCandidate, ...]:
        """Resolve each RAG hit against the ``suppliers`` table, deduped."""
        candidates: list[SupplierCandidate] = []
        seen: set[tuple[int, str]] = set()
        unmapped: list[str] = []
        for product in products:
            code = (product.codigo_proveedor or "").strip()
            if not code:
                continue
            supplier = session.scalar(select(Supplier).where(Supplier.code == code))
            if supplier is None or supplier.status != SupplierStatus.ACTIVO:
                logger.warning(
                    "rag hit dropped: unknown or inactive supplier code=%r", code
                )
                if code not in unmapped:
                    unmapped.append(code)
                continue
            key = (supplier.id, product.sku)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                SupplierCandidate(
                    supplier_id=supplier.id,
                    business_name=supplier.business_name,
                    sku=product.sku,
                    description=product.name,
                    available_quantity=None,
                    status="ACTIVO",
                )
            )
        self.last_unmapped_codes = tuple(unmapped)
        return tuple(candidates)
