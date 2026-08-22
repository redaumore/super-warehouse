"""Supplier document OCR (task 3.9): remito/invoice extraction + price lists.

Ingests supplier purchase documents (remitos/invoices as photos, price lists
as photos or text) by extracting items, quantities and supplier costs through
the VisionAnalyzer protocol (real impl: GPT-4o Vision) plus a deterministic
line parser.

Per the supplier-document-ingestion spec:

- extraction that yields NO usable item lines raises ``IllegibleDocumentError``
  (a handwritten document of very low legibility is out of MVP scope) — the
  owner is routed to manual entry and nothing is written;
- partial extraction is NOT silently discarded: unparsed lines are returned in
  ``DocumentExtraction.unparsed_lines`` and only confirmed lines are eligible
  for entry;
- price-list rows map to an existing catalog SKU when possible (code or name
  match), otherwise the row suggests a new SKU for owner confirmation; the
  mapping rows are stored in ``proveedor_sku_mapping``.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.disambiguation import normalize_text
from src.agents.perception import VisionAnalyzer, analyze_image
from src.db.models import Catalogo, ProveedorSkuMapping

DEFAULT_DOC_PROMPT = (
    "Extract the line items of this supplier document (remito, invoice or price "
    "list). For each line give: code (if present), description, quantity and "
    "supplier cost. Keep the original line order."
)

# "10 x Clavos Paris 2 Pulgadas" — quantity then description.
_QTY_DESC_RE = re.compile(r"^\s*(\d+)\s*[x×]\s*(.+?)\s*$")
# "CLV-001 Clavos Paris 10 1250,00" — code, description, quantity, cost.
_CODE_QTY_COST_RE = re.compile(r"^\s*(\S+)\s+(.+?)\s+(\d+)\s+([\d.,]+)\s*$")
# "CLV-001 Clavos Paris 10" — code, description, quantity (no cost).
_CODE_QTY_RE = re.compile(r"^\s*(\S+)\s+(.+?)\s+(\d+)\s*$")
# Price-list line: "CLV-001 Clavos Paris 1250,00" — code, description, cost.
_PRICE_LIST_RE = re.compile(r"^\s*(\S+)\s+(.+?)\s+([\d.,]+)\s*$")
# Document header/total labels that are never item rows.
_HEADER_RE = re.compile(r"^\s*(?:remito|factura|cliente|fecha|total|firma)[\s:]", re.IGNORECASE)


class IllegibleDocumentError(Exception):
    """The document could not be reliably extracted — route to manual entry."""


@dataclass(frozen=True)
class ExtractedItem:
    """One extracted line: code/description/quantity/supplier cost."""

    codigo: str
    descripcion: str
    cantidad: int
    costo: Decimal | None = None


@dataclass(frozen=True)
class DocumentExtraction:
    """Parsed lines plus the lines that could not be parsed (flagged, kept)."""

    items: tuple[ExtractedItem, ...]
    unparsed_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceListRow:
    """One price-list row: supplier code, description and supplier cost."""

    codigo: str
    descripcion: str
    costo: Decimal


@dataclass(frozen=True)
class PriceListIngestResult:
    """Outcome of ingesting a price list: matched vs suggested-new SKUs."""

    mapped: int
    suggested: int


def _parse_decimal(raw: str) -> Decimal:
    """Parse '1250,00' / '1.250,00' / '1250.00' into a Decimal."""
    normalized = raw.replace(".", "").replace(",", ".") if "," in raw else raw
    return Decimal(normalized)


def _parse_line(line: str) -> ExtractedItem | None:
    match = _QTY_DESC_RE.match(line)
    if match:
        return ExtractedItem(codigo="", descripcion=match.group(2), cantidad=int(match.group(1)))
    match = _CODE_QTY_COST_RE.match(line)
    if match:
        return ExtractedItem(
            codigo=match.group(1),
            descripcion=match.group(2),
            cantidad=int(match.group(3)),
            costo=_parse_decimal(match.group(4)),
        )
    match = _CODE_QTY_RE.match(line)
    if match:
        return ExtractedItem(codigo=match.group(1), descripcion=match.group(2), cantidad=int(match.group(3)))
    return None


def parse_line_items(text: str) -> DocumentExtraction:
    """Parse vision/OCR text into item rows, keeping unparsed lines flagged."""
    items: list[ExtractedItem] = []
    unparsed: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _HEADER_RE.match(line):
            continue
        item = _parse_line(line)
        if item is None:
            unparsed.append(line)
        else:
            items.append(item)
    return DocumentExtraction(items=tuple(items), unparsed_lines=tuple(unparsed))


def parse_price_list(text: str) -> list[PriceListRow]:
    """Parse price-list text into code/description/cost rows."""
    rows: list[PriceListRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _PRICE_LIST_RE.match(line)
        if match:
            rows.append(
                PriceListRow(
                    codigo=match.group(1),
                    descripcion=match.group(2),
                    costo=_parse_decimal(match.group(3)),
                )
            )
    return rows


def image_to_data_url(path: str | Path) -> str:
    """Encode a local image as a data URL for the Vision API."""
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def extract_document(
    analyzer: VisionAnalyzer,
    image_path: str | Path,
    *,
    prompt: str | None = None,
) -> DocumentExtraction:
    """Vision-analyze a supplier document photo and parse its line items.

    Raises ``IllegibleDocumentError`` when nothing usable comes back (the
    document is routed to manual entry and nothing is written to inventory).
    """
    vision = analyze_image(analyzer, image_to_data_url(image_path), prompt or DEFAULT_DOC_PROMPT)
    extraction = parse_line_items(vision.text)
    if not extraction.items:
        raise IllegibleDocumentError(
            "no usable item lines extracted — the document may be illegible"
        )
    return extraction


def _match_existing_sku(session: Session, codigo: str, descripcion: str) -> str | None:
    """Find an existing catalog SKU by supplier code or normalized name."""
    by_code = session.scalar(
        select(Catalogo.codigo_interno).where(
            Catalogo.codigo_interno == codigo.strip().upper()
        )
    )
    if by_code is not None:
        return str(by_code)
    needle = normalize_text(descripcion)
    for sku, nombre in session.execute(
        select(Catalogo.codigo_interno, Catalogo.nombre_oficial)
    ):
        if needle and needle == normalize_text(nombre):
            return str(sku)
    return None


def ingest_price_list_rows(
    session: Session,
    proveedor_id: int,
    rows: list[PriceListRow],
) -> PriceListIngestResult:
    """Store supplier→internal SKU mappings; suggest new SKUs when unmapped.

    Existing mappings for the same (proveedor, codigo_proveedor) are updated,
    never duplicated. Suggested SKUs use the supplier code as the internal
    proposal and need owner confirmation to create the catalog product.
    """
    mapped = 0
    suggested = 0
    for row in rows:
        sku = _match_existing_sku(session, row.codigo, row.descripcion)
        if sku is None:
            sku = row.codigo.strip().upper()
            suggested += 1
            confianza = Decimal("0.50")
        else:
            mapped += 1
            confianza = Decimal("0.90")
        existing = session.scalar(
            select(ProveedorSkuMapping).where(
                ProveedorSkuMapping.proveedor_id == proveedor_id,
                ProveedorSkuMapping.codigo_proveedor == row.codigo,
            )
        )
        if existing is None:
            session.add(
                ProveedorSkuMapping(
                    proveedor_id=proveedor_id,
                    codigo_proveedor=row.codigo,
                    descripcion_raw=row.descripcion,
                    sku_interno=sku,
                    confianza=confianza,
                )
            )
        else:
            existing.sku_interno = sku
            existing.descripcion_raw = row.descripcion
            existing.confianza = confianza
    session.flush()
    return PriceListIngestResult(mapped=mapped, suggested=suggested)