"""Supplier SKU mappings: supplier-facing codes resolve to internal SKUs.

Design decision (user-approved): ``catalogo.codigo_interno`` stays an OPAQUE
internal identifier — it is never repointed, rewritten or derived from a
different supplier code. Supplier-facing codes (receipt codes, price-list
codes, RAG ``codigo_orig`` values) become searchable through
``supplier_sku_mappings``: each row maps one supplier's raw code to the
internal SKU of the catalog product it identifies.

The mapping is written at every point where a supplier code and a product meet
(adoption, receipt ingestion, one-off backfill) and read by existing-product
detection during ingestion (``ingestion.py``) and by the order-form search
(``product_search.py``). Codes are stored NORMALIZED (``normalize_supplier_code``)
so lookups and ILIKE search behave consistently regardless of how the code was
typed on the document.

Writes are idempotent with a first-mapping-wins rule: an existing
(supplier, code) row is never silently repointed to a different internal SKU.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Catalogo, SupplierSkuMapping

_DEFAULT_CONFIDENCE = Decimal(100)


def normalize_supplier_code(code: str) -> str:
    """Normalize a supplier code: strip, upper, collapse whitespace runs.

    ``"AX  302-8"`` and ``"  ax 302-8 "`` both normalize to ``"AX 302-8"``,
    so the same physical code typed differently resolves to one mapping.
    """
    return re.sub(r"\s+", " ", code.strip().upper())


def record_supplier_sku(
    session: Session,
    supplier_id: int,
    supplier_sku_code: str,
    internal_sku: str,
    *,
    raw_description: str | None = None,
    confidence: Decimal | None = None,
) -> None:
    """Idempotently record a supplier-code → internal-SKU mapping.

    - No row for (supplier_id, normalized code) → insert one.
    - Row exists pointing at the SAME internal SKU → no-op.
    - Row exists pointing at a DIFFERENT internal SKU → left untouched
      (first mapping wins; no silent repointing).
    - Blank code → no-op (nothing searchable to record).

    Stores the normalized code and defaults ``confidence`` to 100
    (owner-confirmed mapping). Session-in / caller-commits: rows are flushed,
    never committed — the caller owns the transaction.
    """
    normalized = normalize_supplier_code(supplier_sku_code)
    if not normalized:
        return
    existing = session.scalar(
        select(SupplierSkuMapping).where(
            SupplierSkuMapping.supplier_id == supplier_id,
            SupplierSkuMapping.supplier_sku_code == normalized,
        )
    )
    if existing is not None:
        return  # same or conflicting internal_sku: the first mapping wins
    session.add(
        SupplierSkuMapping(
            supplier_id=supplier_id,
            supplier_sku_code=normalized,
            raw_description=raw_description,
            internal_sku=internal_sku,
            confidence=confidence if confidence is not None else _DEFAULT_CONFIDENCE,
        )
    )
    session.flush()


def find_product_by_any_supplier_code(
    session: Session, code: str
) -> Catalogo | None:
    """Resolve a code to its catalog product across ALL suppliers' mappings.

    Unlike ``find_product_by_supplier_code`` this does not need the supplier
    id (the caller only has the typed code, e.g. the Productos edit box).
    When the same normalized code is mapped by several suppliers the mapping
    with the earliest id wins (deterministic, first-recorded mapping).
    Returns ``None`` when the code is blank or unmapped.
    """
    normalized = normalize_supplier_code(code)
    if not normalized:
        return None
    return session.scalar(
        select(Catalogo)
        .join(
            SupplierSkuMapping,
            SupplierSkuMapping.internal_sku == Catalogo.codigo_interno,
        )
        .where(SupplierSkuMapping.supplier_sku_code == normalized)
        .order_by(SupplierSkuMapping.id)
        .limit(1)
    )


def primary_supplier_codes(
    session: Session, internal_skus: Sequence[str]
) -> dict[str, str]:
    """Primary mapped supplier code per internal SKU, in one query (no N+1).

    The primary mapping is the one with the highest ``confidence``, ties
    broken by the earliest ``id``. SKUs without any mapping are absent from
    the result (callers decide the fallback, e.g. empty string).
    """
    skus = [sku for sku in internal_skus if sku]
    if not skus:
        return {}
    rows = session.execute(
        select(
            SupplierSkuMapping.internal_sku,
            SupplierSkuMapping.supplier_sku_code,
        )
        .where(SupplierSkuMapping.internal_sku.in_(skus))
        .order_by(SupplierSkuMapping.confidence.desc(), SupplierSkuMapping.id)
    )
    primary: dict[str, str] = {}
    for internal_sku, code in rows:
        if internal_sku not in primary:  # first row per SKU = highest rank
            primary[internal_sku] = code
    return primary


def find_product_by_supplier_code(
    session: Session, supplier_id: int, code: str
) -> Catalogo | None:
    """Resolve a supplier's code to its catalog product via the mapping table.

    Returns ``None`` when the code is blank, unmapped for the supplier, or the
    mapped internal SKU has no live ``catalogo`` row.
    """
    normalized = normalize_supplier_code(code)
    if not normalized:
        return None
    return session.scalar(
        select(Catalogo).join(
            SupplierSkuMapping,
            SupplierSkuMapping.internal_sku == Catalogo.codigo_interno,
        ).where(
            SupplierSkuMapping.supplier_id == supplier_id,
            SupplierSkuMapping.supplier_sku_code == normalized,
        )
    )
