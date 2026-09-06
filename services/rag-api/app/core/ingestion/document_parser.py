"""Receipt/invoice document parser with a dedicated receipt prompt.

Design D1: a page-render adapter turns the uploaded document into pages — a PDF
is rendered page-by-page with pymupdf (text layer + base64 PNG at 200 dpi), an
image is wrapped as a single page (``pagina=1``, empty text layer). Every page
is sent to the model with a DEDICATED receipt prompt and structured-output
schema (NOT the catalog prompt: catalog filters unpriced/unavailable rows and
has no quantity field). The endpoint never persists anything — parse is a pure
no-write operation.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

import pymupdf as fitz
from openai import OpenAI
from pydantic import BaseModel, Field

from app.api.schemas.ingest import DocumentLine, ParsedDocument

logger = logging.getLogger("DocumentLineParser")

_DEFAULT_MODEL = "gpt-5.6-luna"
_RENDER_DPI = 200


class ReceiptLine(BaseModel):
    """Per-page receipt line extracted by the model (no page number — per page)."""

    codigo_orig: str | None = Field(
        default=None, description="Código de artículo del proveedor tal como figura en el remito/factura"
    )
    codigo: str | None = Field(default=None, description="Código interno/legible del artículo si existe")
    descripcion: str = Field(description="Descripción del artículo")
    cantidad: int = Field(description="Cantidad recibida (número entero)")
    costo: float | None = Field(default=None, description="Costo unitario del proveedor; null si no figura")


class ReceiptPageResult(BaseModel):
    """Structured output for one page: the receipt lines found on it."""

    lineas: list[ReceiptLine] = Field(default_factory=list)


_RECEIPT_SYSTEM_PROMPT = (
    "Eres un motor especialista en Document AI para remitos y facturas de proveedor.\n\n"
    "Tu objetivo es transformar CADA artículo del documento en un registro JSON estructurado.\n\n"
    "=== REGLAS OBLIGATORIAS ===\n"
    "1. 'codigo_orig': extrae el código literal del artículo tal como figura en el documento. "
    "Si el artículo no tiene código explícito, usa null.\n"
    "2. 'descripcion': descripción clara y completa del artículo.\n"
    "3. 'cantidad': cantidad recibida como número entero (si figura '10 x ...', la cantidad es 10).\n"
    "4. 'costo': costo unitario del proveedor como número; null si el documento no lo muestra.\n"
    "5. PROHIBIDO inventar códigos, cantidades o costos: extrae solo lo que figura.\n"
    "6. NO omitas artículos por falta de precio: a diferencia de un catálogo, un remito "
    "puede listar artículos sin costo y todos deben extraerse.\n"
    "7. Si una fila es ilegible o ambigua, extrae lo legible y deja en null lo incierto.\n"
)


@dataclass(frozen=True)
class RenderedPage:
    """One rendered page fed to the model: text layer + base64 PNG."""

    pagina: int
    text: str
    image_b64: str | None = None


def _get_openai_client(api_key: str | None = None) -> OpenAI:
    """Build the OpenAI client from ``OPENAI_API_KEY`` (embedder pattern).

    Mirrors ``core/ingestion/embedder.py::get_openai_client`` without importing
    that module (it pulls numpy/torch/transformers, which are not runtime deps
    of the API service).
    """
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError(
            "OpenAI API Key no encontrada. Configúrala mediante OPENAI_API_KEY o en el archivo .env."
        )
    return OpenAI(api_key=key)


def _guess_image_mime(filename: str) -> str:
    """Best-effort MIME type for an image upload (defaults to PNG)."""
    lower = (filename or "").lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/png"


def _render_pages(content: bytes, filename: str) -> list[RenderedPage]:
    """Render an uploaded document into pages for the model (design D1).

    PDF → pymupdf page-by-page (text + base64 PNG @200dpi). Image → single page
    (``pagina=1``, empty text layer, raw bytes as base64 data URL). Everything
    stays in memory — nothing is written to disk.
    """
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        pages: list[RenderedPage] = []
        with fitz.open(stream=content, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                pix = page.get_pixmap(dpi=_RENDER_DPI)
                image_b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
                text = str(page.get_text() or "")
                pages.append(RenderedPage(pagina=index + 1, text=text, image_b64=image_b64))
        if not pages:
            raise ValueError("document has no readable pages")
        return pages
    # Image upload → single page, no text layer (design D1).
    mime = _guess_image_mime(filename)
    data_url = f"data:{mime};base64,{base64.b64encode(content).decode('utf-8')}"
    return [RenderedPage(pagina=1, text="", image_b64=data_url)]


class DocumentLineParser:
    """Parse a supplier remito/invoice into structured lines (no writes)."""

    def __init__(self, client: OpenAI | None = None, model: str = _DEFAULT_MODEL) -> None:
        self.client = client or _get_openai_client()
        self.model = model

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        """Parse ``content`` (bytes in memory) into typed lines + source metadata."""
        pages = _render_pages(content, filename)
        lines: list[DocumentLine] = []
        for page in pages:
            page_result = self._parse_page(page)
            for raw in page_result.lineas:
                lines.append(
                    DocumentLine(
                        codigo_orig=raw.codigo_orig,
                        codigo=raw.codigo,
                        descripcion=raw.descripcion,
                        cantidad=raw.cantidad,
                        costo=raw.costo,
                        pagina=page.pagina,
                    )
                )
        return ParsedDocument(lines=lines, source_filename=filename, source_pages=len(pages))

    def _parse_page(self, page: RenderedPage) -> ReceiptPageResult:
        """Send one rendered page to the model with the receipt schema."""
        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"--- PÁGINA {page.pagina} DEL DOCUMENTO ---\n\n"
                    f"TEXTO EXTRAÍDO POR CAPA VECTORIAL:\n{page.text or '[Página visual/escaneada]'}"
                ),
            }
        ]
        if page.image_b64:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": page.image_b64 if page.image_b64.startswith("data:") else (
                            f"data:image/png;base64,{page.image_b64}"
                        ),
                        "detail": "high",
                    },
                }
            )
        logger.info("Parsing document page %d with %s", page.pagina, self.model)
        response = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": _RECEIPT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},  # type: ignore[arg-type]
            ],
            response_format=ReceiptPageResult,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError(f"no structured parse result for page {page.pagina}")
        return parsed