"""Request/response DTOs for the document-ingestion endpoints.

These are the API wire models only. The core parser in
``app.core.ingestion.document_parser`` defines its own dedicated receipt
structured-output schema; the endpoint maps that into these response DTOs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentLine(BaseModel):
    """One parsed line from a supplier remito/invoice.

    ``codigo_orig`` is the raw supplier article code on the document;
    ``codigo`` is any internal/legible code the model could read. ``costo`` is
    the unit supplier cost when present (remitos may omit it). ``pagina`` is the
    1-indexed source page (always 1 for an image upload).
    """

    codigo_orig: str | None = Field(default=None, description="Código de artículo original del proveedor")
    codigo: str | None = Field(default=None, description="Código interno/legible del artículo")
    descripcion: str = Field(description="Descripción del artículo")
    cantidad: int = Field(description="Cantidad recibida")
    costo: float | None = Field(default=None, description="Costo unitario del proveedor")
    pagina: int = Field(description="Página de origen (1-indexed; 1 para imágenes)")


class ParsedDocument(BaseModel):
    """A parsed supplier document: structured lines plus source metadata."""

    lines: list[DocumentLine] = Field(default_factory=list)
    source_filename: str = Field(description="Nombre del archivo de origen")
    source_pages: int = Field(description="Cantidad de páginas procesadas")


class DocumentParseResponse(BaseModel):
    """Successful parse response. The parse endpoint never writes."""

    document: ParsedDocument


class ProductLookupResponse(BaseModel):
    """One full row of ``catalogo_productos_rag`` incl. its ``node_id``.

    Used by both the single-row price lookup and the exact-code ingestion
    resolution (which returns all matching rows for ambiguity detection).
    """

    codigo_orig: str | None = None
    codigo: str | None = None
    codigo_proveedor: str | None = None
    nombre_proveedor: str | None = None
    nombre: str | None = None
    descripcion: str | None = None
    marca: str | None = None
    categoria: str | None = None
    subcategoria: str | None = None
    precio: float | None = None
    moneda: str | None = None
    pagina_origen: int | None = None
    archivo_origen: str | None = None
    node_id: str | None = None


class ExactLookupResponse(BaseModel):
    """All rows matching an exact ``codigo_orig`` scoped to a supplier."""

    matches: list[ProductLookupResponse] = Field(default_factory=list)
