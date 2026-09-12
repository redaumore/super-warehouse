"""Document-ingestion endpoints (rag-document-ingestion change).

``POST /ingest/parse``: multipart receipt/invoice → structured lines. The upload
is read fully in memory and NEVER persisted; parse errors return a structured
HTTP error with zero writes.

``GET /catalog/exact``: exact ``codigo_orig`` lookup scoped to a supplier code,
using ``UPPER(TRIM(codigo_orig))`` on both sides. Returns ALL matching rows with
``node_id`` so callers can detect duplicates (design D4).

``GET /products/{sku}``: full-row lookup by supplier article code. Returns 404
when no row matches (price lookup maps that to ``None``) and an array of ALL
matching rows otherwise — the ingestion exact lookup reuses this route for
ambiguity detection (design D5).
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from psycopg import sql
from psycopg.rows import dict_row

from app.api.schemas.ingest import (
    DocumentParseResponse,
    ExactLookupResponse,
    ProductLookupResponse,
)
from app.config import settings
from app.core.ingestion.document_parser import DocumentLineParser, _get_openai_client

router = APIRouter()
logger = logging.getLogger("IngestionEndpoint")

_EXACT_COLUMNS = (
    "node_id, codigo_producto, codigo_orig, marca, categoria_padre, categoria, "
    "subcategoria, nombre_proveedor, codigo_proveedor, precio, moneda, pagina_origen, "
    "text_content, metadata"
)


def _extract_text_field(text_content: str | None, key: str) -> str | None:
    """Read one ``key: value`` line from the node's YAML-ish ``text_content``.

    The product display name and description are not first-class columns of
    ``catalogo_productos_rag`` — ``chunker.py`` embeds them as ``nombre:`` /
    ``descripcion:`` lines in ``text_content`` (the text that was embedded).
    """
    if not text_content:
        return None
    prefix = f"{key}:"
    for line in text_content.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :].strip()
            return value or None
    return None


def _get_parser() -> DocumentLineParser:
    """Build the parse endpoint's parser (patchable in tests)."""
    return DocumentLineParser(client=_get_openai_client())


def _exact_lookup_sql(table_name: str, *, scoped: bool) -> sql.Composed:
    """SQL for the exact ``codigo_orig`` lookup, scoped to supplier when asked.

    Normalization matches ``_normalize_sku_part`` (design D4): both sides are
    ``UPPER(TRIM(...))`` so case and surrounding whitespace never break a match.
    """
    query = sql.SQL(
        "SELECT {cols} FROM {table} WHERE UPPER(TRIM(codigo_orig)) = UPPER(TRIM(%s))"
    ).format(cols=sql.SQL(_EXACT_COLUMNS), table=sql.Identifier(table_name))
    if scoped:
        query = query + sql.SQL(" AND codigo_proveedor = %s")
    return query + sql.SQL(" ORDER BY node_id")


def _fetch_exact_matches(
    codigo: str, codigo_proveedor: str | None, table_name: str
) -> list[dict[str, Any]]:
    """Query all ``catalogo_productos_rag`` rows matching the code (no write)."""
    params: list[Any] = [codigo]
    scoped = bool(codigo_proveedor and codigo_proveedor.strip())
    if scoped:
        params.append(codigo_proveedor.strip())
    query = _exact_lookup_sql(table_name, scoped=scoped)
    db_url = settings.get_db_url()
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return list(cur.fetchall())


def _row_to_product(row: dict[str, Any]) -> ProductLookupResponse:
    """Map one catalog row (dict_row) into the typed response DTO."""
    metadata = row.get("metadata")
    raw_metadata = metadata if isinstance(metadata, dict) else {}
    text_content = row.get("text_content")
    return ProductLookupResponse(
        codigo_orig=row.get("codigo_orig"),
        codigo=row.get("codigo_producto"),
        codigo_proveedor=row.get("codigo_proveedor"),
        nombre_proveedor=row.get("nombre_proveedor"),
        nombre=_extract_text_field(text_content, "nombre"),
        descripcion=_extract_text_field(text_content, "descripcion"),
        marca=row.get("marca"),
        categoria=row.get("categoria"),
        subcategoria=row.get("subcategoria"),
        precio=float(row["precio"]) if row.get("precio") is not None else None,
        moneda=row.get("moneda"),
        pagina_origen=row.get("pagina_origen"),
        archivo_origen=raw_metadata.get("archivo_origen"),
        node_id=row.get("node_id"),
    )


@router.post(
    "/ingest/parse",
    response_model=DocumentParseResponse,
    summary="Parsear remito/factura a líneas estructuradas (sin escrituras)",
)
def parse_document(
    file: UploadFile = File(..., description="Remito/factura (PDF o imagen)"),
    # Optional: the parser is supplier-agnostic (it never reads this value).
    # Required was a contract bug — FastAPI treats an empty multipart Form
    # value as MISSING, so supplier-less parses (placeholder selection) 422ed.
    codigo_proveedor: str | None = Form(
        None, description="Código del proveedor (opcional; el parser no lo usa)"
    ),
) -> DocumentParseResponse:
    """Parse an uploaded supplier document into structured lines. No writes."""
    content = file.file.read()  # bytes in memory — the endpoint never persists
    filename = file.filename or "documento"
    if not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty document")
    try:
        parser = _get_parser()
        parsed = parser.parse(content, filename)
    except ValueError as exc:
        logger.warning("parse failed for %r: %s", filename, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"parse failed: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — structured error, zero writes
        logger.error("parse error for %r: %s", filename, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"parse error: {exc}"
        ) from exc
    return DocumentParseResponse(document=parsed)


@router.get(
    "/catalog/exact",
    response_model=ExactLookupResponse,
    summary="Lookup exacto de codigo_orig por proveedor (todas las filas con node_id)",
)
def exact_lookup(
    codigo_orig: str,
    codigo_proveedor: str,
    table_name: str = settings.DEFAULT_TABLE_NAME,
) -> ExactLookupResponse:
    """All rows whose ``codigo_orig`` matches exactly, scoped to the supplier."""
    rows = _fetch_exact_matches(codigo_orig, codigo_proveedor, table_name)
    return ExactLookupResponse(matches=[_row_to_product(r) for r in rows])


@router.get(
    "/products/{sku}",
    response_model=list[ProductLookupResponse],
    summary="Lookup por SKU/codigo_orig: 404 si no hay filas, array con todas las coincidencias",
)
def product_lookup(
    sku: str,
    codigo_proveedor: str | None = None,
    table_name: str = settings.DEFAULT_TABLE_NAME,
) -> list[ProductLookupResponse]:
    """Full row(s) for a supplier article code; 404 when nothing matches.

    Price lookup (single row → ``None`` on 404) and ingestion exact resolution
    (all matches for ambiguity detection) share this route (design D5).
    """
    rows = _fetch_exact_matches(sku, codigo_proveedor, table_name)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    return [_row_to_product(r) for r in rows]