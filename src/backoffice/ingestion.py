"""Backoffice supplier document ingestion (task 3.6).

Upload → Vision analyze → preview grid → confirm flow. The extraction step
delegates to the supplier OCR module (VisionAnalyzer + line parser); the
preview grid is an editable Dataframe — the owner can correct fields before
confirming, and ONLY the confirmed (possibly corrected) rows reach inventory.

``confirm_items`` never guesses: each row maps to an existing catalog SKU when
it matches (code or normalized name), otherwise a new product is created with
the supplier's default margin and the base price recomputed through the pure
pricing engine. Extraction failures raise before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.disambiguation import normalize_text
from src.agents.perception import VisionAnalyzer
from src.db.models import Catalogo, Inventory, Proveedor
from src.pricing.engine import compute_base
from src.supplier.ocr import DocumentExtraction, extract_document

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class ConfirmedIngest:
    """Outcome of confirming a preview grid: existing updated vs new created."""

    updated: int
    created: int


def extract_document_items(analyzer: VisionAnalyzer, image_path: str | Path) -> DocumentExtraction:
    """Vision-analyze an uploaded supplier document and parse its line items."""
    return extract_document(analyzer, image_path)


def to_grid_rows(extraction: DocumentExtraction) -> list[list[str]]:
    """Render extracted items as editable Dataframe rows (code, desc, qty, cost)."""
    return [
        [item.codigo, item.descripcion, str(item.cantidad), str(item.costo or "")]
        for item in extraction.items
    ]


def _find_existing_product(session: Session, codigo: str, descripcion: str) -> Catalogo | None:
    code = codigo.strip().upper()
    if code:
        product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == code))
        if product is not None:
            return product
    needle = normalize_text(descripcion)
    for product in session.scalars(select(Catalogo)):
        if needle and needle == normalize_text(product.nombre_oficial):
            return product
    return None


def confirm_items(
    session: Session,
    rows: list[list[object]],
    proveedor_id: int,
) -> ConfirmedIngest:
    """Write the confirmed grid into inventory/catalog; never on empty rows."""
    proveedor = session.get(Proveedor, proveedor_id)
    if proveedor is None:
        raise KeyError(f"unknown supplier: {proveedor_id}")
    updated = 0
    created = 0
    for row in rows:
        codigo = str(row[0] or "").strip()
        descripcion = str(row[1] or "").strip()
        if not codigo and not descripcion:
            continue
        try:
            cantidad = int(float(str(row[2] or 0)))
        except (TypeError, ValueError):
            raise ValueError(f"invalid quantity in row: {row}") from None
        if cantidad <= 0:
            continue
        costo = _coerce_cost(row[3])
        product = _find_existing_product(session, codigo, descripcion)
        if product is not None:
            product.stock_disponible += cantidad
            # Mirror the supplier confirmation into the canonical on-hand source.
            inventory_row = session.scalar(
                select(Inventory).where(Inventory.sku_id == product.codigo_interno)
            )
            if inventory_row is not None:
                inventory_row.quantity_on_hand += cantidad
                inventory_row.updated_at = datetime.now(UTC)
            else:
                session.add(Inventory(sku_id=product.codigo_interno, quantity_on_hand=cantidad))
            if costo is not None:
                product.costo_proveedor = costo
                product.precio_lista_base = compute_base(costo, product.margen_aplicado_pct)
            updated += 1
        else:
            sku = codigo or f"NEW-{len(session.scalars(select(Catalogo)).all()) + 1:04d}"
            margen = proveedor.margen_predeterminado
            costo_final = costo or Decimal(0)
            session.add(
                Catalogo(
                    codigo_interno=sku,
                    proveedor_id=proveedor_id,
                    nombre_oficial=descripcion,
                    costo_proveedor=costo_final,
                    margen_aplicado_pct=margen,
                    precio_lista_base=compute_base(costo_final, margen),
                    stock_disponible=cantidad,
                    sinonimos=[descripcion],
                )
            )
            session.add(Inventory(sku_id=sku, quantity_on_hand=cantidad))
            created += 1
    session.flush()
    return ConfirmedIngest(updated=updated, created=created)


def _coerce_cost(raw: object) -> Decimal | None:
    """Coerce a grid cost cell to Decimal; None when blank or unparseable."""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    try:
        return Decimal(text).quantize(_CENT)
    except Exception:  # noqa: BLE001 — owner will see the correction in the grid
        return None
