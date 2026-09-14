"""One-off idempotent backfill of supplier_sku_mappings from catalogo provenance.

For every existing ``catalogo`` row, extracts the supplier codes already
recorded in its write-once ``origen`` JSONB and records them as
``SupplierSkuMapping`` rows pointing at the row's ``codigo_interno``:

- ``origen["remito"]["linea"]["codigo_orig"]`` — the code printed on the
  receipt that created/adopted the product;
- for ``origen["rag"]["node_id"]`` — the RAG product's ``codigo_producto`` and
  ``codigo_orig`` columns, looked up read-only in the rag-api-owned table
  (``settings.rag_table_name``); the RAG table is never written.

Mappings already present (same supplier + normalized code) are skipped —
``record_supplier_sku``'s first-mapping-wins rule means existing rows are never
repointed. Every inserted row carries confidence 100 and a raw_description
noting the backfill source.

Usage:
    .venv/bin/python scripts/backfill_supplier_sku_mappings.py
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.backoffice.sku_mappings import normalize_supplier_code, record_supplier_sku
from src.config import get_settings
from src.db.models import Catalogo, SupplierSkuMapping
from src.db.session import SessionLocal
from src.sourcing.product_search import rag_products_table

_BACKFILL_CONFIDENCE = Decimal(100)


def _supplier_codes(session: Session, product: Catalogo) -> list[tuple[str, str]]:
    """Supplier codes recorded in the row's ``origen`` JSONB, with source tags.

    Returns ``(code, source)`` pairs; ``source`` labels the provenance leg for
    the mapping's ``raw_description``. Codes are returned as found — the
    normalization happens inside ``record_supplier_sku``.
    """
    codes: list[tuple[str, str]] = []
    origen: dict[str, Any] | None = product.origen
    if not origen:
        return codes
    remito_code = ((origen.get("remito") or {}).get("linea") or {}).get("codigo_orig")
    if remito_code and str(remito_code).strip():
        codes.append((str(remito_code), "backfill:remito"))
    node_id = (origen.get("rag") or {}).get("node_id")
    if node_id and str(node_id).strip():
        table = rag_products_table(get_settings().rag_table_name)
        rag_row = session.execute(
            select(table.c.codigo_producto, table.c.codigo_orig).where(
                table.c.node_id == str(node_id)
            )
        ).first()
        if rag_row is not None:
            for value in (rag_row.codigo_producto, rag_row.codigo_orig):
                if value and str(value).strip():
                    codes.append((str(value), "backfill:rag"))
    return codes


def _has_mapping(session: Session, supplier_id: int, code: str) -> bool:
    """True when the supplier already has a mapping for the normalized code."""
    return (
        session.scalar(
            select(SupplierSkuMapping.id).where(
                SupplierSkuMapping.supplier_id == supplier_id,
                SupplierSkuMapping.supplier_sku_code == normalize_supplier_code(code),
            )
        )
        is not None
    )


def main() -> None:
    inserted = 0
    with SessionLocal() as session:
        products = session.scalars(select(Catalogo)).all()
        for product in products:
            for code, source in _supplier_codes(session, product):
                if _has_mapping(session, product.supplier_id, code):
                    continue
                record_supplier_sku(
                    session,
                    product.supplier_id,
                    code,
                    product.codigo_interno,
                    raw_description=f"{source}: {product.nombre_oficial}",
                    confidence=_BACKFILL_CONFIDENCE,
                )
                inserted += 1
        session.commit()
    print(
        f"Supplier SKU mappings backfilled: {inserted} row(s) inserted "
        f"(existing mappings untouched)."
    )


if __name__ == "__main__":
    main()
