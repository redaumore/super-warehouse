"""Supplier-catalog RAG integration (rag-product-query change).

A synchronous ``httpx`` client for the sibling ``fase-0-pdf-parsing`` service
(``POST /api/v1/query`` with ``structured_json=true``). The transport is
injectable for tests (``httpx.MockTransport``) and the client is built lazily
from ``Settings`` on first use — mirroring the ``OpenAIResponder`` pattern in
``integrations/openai.py``.

The service maps ``structured_json.productos[]`` into typed ``RagProduct``
results carrying code, name, provider, brand, price, specs and the source
page/PDF. Each product also resolves its provenance — ``node_id`` and the
category fields — from the matching ``context_chunks`` entry via
``fragmento_id``/``fragment_id``, so adoption can persist ``catalogo.origen``
without ever mutating the sibling service. Failures surface as domain errors
(``RagProductError``), never as raw transport exceptions: a timeout, a
connection error or an HTTP 500 all become ``RagProductError`` so the caller
can answer with an honest unavailability notice instead of a wrong "not in
stock" claim.

``is_refusal=true`` (or an empty product list) means "not in current catalogs"
and maps to an empty result — the RAG ingests only priced, in-stock-at-ingest
products, so "no existe" and "agotado" are the same signal (§5.3 of the catalog
spec) and the consumer must not claim stock status either way.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import Settings, get_settings
from src.observability.session_logger import log_session_event
from src.supplier.rag_catalog import (
    RagPrice,
    RagProduct,
    RagProductError,
    normalize_rag_sku,
)

# Domain vocabulary re-exports: RagProduct/RagPrice/RagProductError and
# normalize_rag_sku are owned by src.supplier.rag_catalog (L1); the adapter
# re-exports them so L2/L3 consumers and tests keep one shared definition.
__all__ = [
    "RAG_CATALOG_DOCUMENTS_PATH",
    "RAG_CATALOG_INGEST_PATH",
    "RAG_INGEST_TIMEOUT_FACTOR",
    "RAG_JOB_STATUS_PATH",
    "DocumentLine",
    "RagDocumentSummary",
    "RagJobStatus",
    "RagPrice",
    "RagProduct",
    "RagProductClient",
    "RagProductError",
    "RagProductNotConfigured",
    "RagProviderDocuments",
    "normalize_rag_sku",
]

logger = logging.getLogger(__name__)

RAG_CATALOG_INGEST_PATH = "/api/v1/catalogs/ingest-file"
RAG_CATALOG_DOCUMENTS_PATH = "/api/v1/catalogs/documents"
RAG_JOB_STATUS_PATH = "/api/v1/jobs/{job_id}"

# Catalog PDFs are big multipart uploads processed by a slow OCR pipeline: the
# upload request gets a per-request timeout scaled up from the base
# ``rag_timeout_seconds`` (the job itself runs async server-side, so only the
# upload+accept round-trip is bounded here).
RAG_INGEST_TIMEOUT_FACTOR = 20.0


def _as_fragment_id(value: Any) -> int | None:
    """Coerce a ``fragment_id``/``fragmento_id`` payload value to int or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _fragment_to_chunk_map(context_chunks: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Map ``fragment_id`` → context chunk (provenance source for products).

    The RAG response links each ``productos[]`` row to a retrieval chunk via
    ``fragmento_id``; the chunk carries ``node_id`` and the category metadata
    that the product row may omit.
    """
    mapping: dict[int, dict[str, Any]] = {}
    for chunk in context_chunks:
        if not isinstance(chunk, dict):
            continue
        fragment_id = _as_fragment_id(chunk.get("fragment_id"))
        if fragment_id is not None:
            mapping[fragment_id] = chunk
    return mapping


@dataclass(frozen=True)
class DocumentLine:
    """One structured line from the RAG document parse endpoint.

    ``codigo_orig`` is the raw supplier article code on the document; ``costo``
    is the unit supplier cost when the document shows it (remitos may omit it);
    ``pagina`` is the 1-indexed source page (always 1 for image uploads).
    """

    codigo_orig: str | None
    codigo: str | None
    descripcion: str
    cantidad: int
    costo: float | None
    pagina: int


@dataclass(frozen=True)
class RagJobStatus:
    """One async ingestion job snapshot from ``GET /api/v1/jobs/{job_id}``.

    ``status`` is the service-side lifecycle value (``PENDING``, ``RUNNING``,
    ``COMPLETED``, ``FAILED``); ``progress_message`` carries the service's
    human progress text, ``result`` the completion payload (ingestion summary
    dict) and ``error`` the failure detail.
    """

    job_id: str
    status: str
    progress_message: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class RagDocumentSummary:
    """One indexed document of a supplier from ``GET /api/v1/catalogs/documents``.

    ``documento_id`` is the operator-declared logical document identity (e.g.
    ``"LISTA GENERAL"``); ``total_productos`` the indexed row count; and
    ``ultimo_archivo``/``actualizado_en`` the last source file and update
    timestamp (ISO 8601) when the service reported them.
    """

    documento_id: str
    total_productos: int
    ultimo_archivo: str | None = None
    actualizado_en: str | None = None


@dataclass(frozen=True)
class RagProviderDocuments:
    """The indexed documents of one supplier (``documentos[]`` of the payload).

    An unknown provider maps to an empty ``documents`` tuple — the service
    answers 200 with an empty list instead of a 404, so the UI treats it as
    "no documents yet", not as an error.
    """

    codigo_proveedor: str
    documents: tuple[RagDocumentSummary, ...] = ()


class RagProductNotConfigured(RagProductError):
    """No base URL is configured; the client cannot be built."""


class _ClientHolder:
    """Lazily builds the ``httpx.Client`` on first use (never at import time).

    The transport is injectable so tests can stub the network boundary with
    ``httpx.MockTransport`` while production builds a real client from settings.
    """

    def __init__(
        self,
        client: httpx.Client | None,
        base_url: str,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            if not self._base_url:
                raise RagProductNotConfigured("rag base url not configured (set RAG_BASE_URL)")
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client


class RagProductClient:
    """Client for the supplier-catalog RAG ``POST /api/v1/query`` endpoint.

    A full ``httpx.Client`` can be injected for OpenAI-style mocking, or a
    ``transport`` for ``httpx.MockTransport``-style stubbing; when neither is
    given the client is built lazily from settings.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._holder = _ClientHolder(
            client,
            base_url=self.settings.rag_base_url,
            timeout=self.settings.rag_timeout_seconds,
            transport=transport,
        )
        self.table_name = self.settings.rag_table_name
        self.top_n = self.settings.rag_top_n
        self.threshold = self.settings.rag_threshold
        self.model = self.settings.rag_model

    def query(self, text: str) -> tuple[RagProduct, ...]:
        """Query the RAG for ``text`` and return typed products, best first.

        Returns an empty tuple when the RAG refuses (``is_refusal=true``) or
        returns no products — "not found in current catalogs", not an error.
        Timeouts, connection failures, non-200 statuses and unparsable payloads
        raise ``RagProductError``.
        """
        started = time.perf_counter()
        client = self._holder.client
        payload = {
            "query": text,
            "table_name": self.table_name,
            "top_n": self.top_n,
            "threshold": self.threshold,
            "structured_json": True,
            "model": self.model,
        }
        try:
            response = client.post("/api/v1/query", json=payload)
        except httpx.HTTPError as exc:
            log_session_event(
                "rag", "query_error", {"query": text, "error": str(exc)}, level="ERROR"
            )
            raise RagProductError(f"rag query failed for {text!r}: {exc}") from exc
        latency = time.perf_counter() - started
        if response.status_code != 200:
            log_session_event(
                "rag",
                "query_error",
                {"query": text, "status": response.status_code, "latency_sec": round(latency, 3)},
                level="WARNING",
            )
            logger.warning("rag query status=%s latency=%.1fs", response.status_code, latency)
            raise RagProductError(f"rag query returned HTTP {response.status_code} for {text!r}")
        try:
            data = response.json()
        except ValueError as exc:
            log_session_event(
                "rag", "query_error", {"query": text, "error": "non-json"}, level="ERROR"
            )
            raise RagProductError(f"rag query returned non-JSON payload: {exc}") from exc
        if data.get("is_refusal"):
            log_session_event(
                "rag", "query_refusal", {"query": text, "latency_sec": round(latency, 3)}
            )
            logger.info("rag refusal for query=%r latency=%.1fs", text, latency)
            return ()
        structured = data.get("structured_json") or {}
        products = structured.get("productos") or []
        fragment_to_chunk = _fragment_to_chunk_map(data.get("context_chunks") or [])
        mapped = tuple(
            self._map_product(product, fragment_to_chunk)
            for product in products
            if product.get("nombre")
        )
        log_session_event(
            "rag",
            "query_success",
            {"query": text, "products_count": len(mapped), "latency_sec": round(latency, 3)},
        )
        logger.info("rag query=%r products=%d latency=%.1fs", text, len(mapped), latency)
        return mapped

    def price_lookup(self, sku: str, codigo_proveedor: str | None = None) -> RagPrice | None:
        """Look up one supplier offer price by SKU and optional supplier code.

        The sibling service returns 404 when it has no matching product. That is
        a normal absence and becomes ``None``; transport failures, malformed
        JSON, and non-2xx responses remain domain errors.
        """
        clean_sku = str(sku or "").strip()
        if not clean_sku:
            raise ValueError("sku is required for RAG price lookup")
        params = (
            {"codigo_proveedor": codigo_proveedor.strip()}
            if codigo_proveedor and codigo_proveedor.strip()
            else None
        )
        try:
            response = self._holder.client.get(f"/api/v1/products/{clean_sku}", params=params)
        except httpx.HTTPError as exc:
            raise RagProductError(f"rag price lookup failed for {clean_sku!r}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RagProductError(
                f"rag price lookup returned HTTP {response.status_code} for {clean_sku!r}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RagProductError(f"rag price lookup returned non-JSON payload: {exc}") from exc
        if isinstance(data, list):
            # The product route returns an array of all matching rows; price
            # lookup consumes the first one (the route 404s when none exist).
            if not data:
                return None
            data = data[0]
        if not isinstance(data, dict):
            raise RagProductError("rag price lookup returned an invalid payload")
        raw_price = data.get("precio")
        if raw_price is not None:
            try:
                raw_price = float(raw_price)
            except (TypeError, ValueError) as exc:
                raise RagProductError("rag price lookup returned an invalid price") from exc
        currency = data.get("moneda")
        return RagPrice(price=raw_price, currency=str(currency).upper() if currency else None)

    def parse_document(
        self, *, filename: str, content: bytes, codigo_proveedor: str
    ) -> tuple[DocumentLine, ...]:
        """Parse a supplier remito/invoice via ``POST /api/v1/ingest/parse``.

        The endpoint never persists the document; transport failures, timeouts
        and non-200 statuses raise ``RagProductError`` (never a raw httpx
        exception), bounded by ``settings.rag_timeout_seconds``.
        """
        if not filename or not content:
            raise ValueError("filename and content are required for document parse")
        try:
            response = self._holder.client.post(
                "/api/v1/ingest/parse",
                files={"file": (filename, content)},
                data={"codigo_proveedor": codigo_proveedor},
            )
        except httpx.HTTPError as exc:
            log_session_event(
                "rag", "parse_error", {"filename": filename, "error": str(exc)}, level="ERROR"
            )
            raise RagProductError(f"rag document parse failed for {filename!r}: {exc}") from exc
        if response.status_code != 200:
            log_session_event(
                "rag",
                "parse_error",
                {"filename": filename, "status": response.status_code},
                level="WARNING",
            )
            raise RagProductError(
                f"rag document parse returned HTTP {response.status_code} for {filename!r}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RagProductError(f"rag document parse returned non-JSON payload: {exc}") from exc
        lines_raw = (data.get("document") or {}).get("lines") or []
        lines: list[DocumentLine] = []
        for raw in lines_raw:
            if not isinstance(raw, dict):
                continue
            try:
                cantidad = int(raw.get("cantidad", 0))
            except (TypeError, ValueError):
                cantidad = 0
            costo = raw.get("costo")
            if costo is not None:
                try:
                    costo = float(costo)
                except (TypeError, ValueError):
                    costo = None
            try:
                pagina = int(raw.get("pagina") or 1)
            except (TypeError, ValueError):
                pagina = 1
            lines.append(
                DocumentLine(
                    codigo_orig=raw.get("codigo_orig"),
                    codigo=raw.get("codigo"),
                    descripcion=str(raw.get("descripcion") or ""),
                    cantidad=cantidad,
                    costo=costo,
                    pagina=pagina,
                )
            )
        log_session_event(
            "rag", "parse_success", {"filename": filename, "lines_count": len(lines)}
        )
        return tuple(lines)

    def exact_lookup(self, codigo_orig: str, codigo_proveedor: str) -> tuple[RagProduct, ...]:
        """Exact ``codigo_orig`` lookup scoped to the supplier (ALL matches).

        Reuses ``GET /api/v1/products/{sku}`` (design D5): the route returns an
        array of every row whose ``codigo_orig`` matches exactly — so duplicate
        codes surface for ambiguity detection instead of silently picking one.
        A 404 (no match) maps to an empty tuple; transport failures, timeouts
        and other non-200 statuses raise ``RagProductError``.
        """
        clean_code = str(codigo_orig or "").strip()
        if not clean_code:
            raise ValueError("codigo_orig is required for exact lookup")
        params = (
            {"codigo_proveedor": codigo_proveedor.strip()}
            if codigo_proveedor and codigo_proveedor.strip()
            else None
        )
        try:
            response = self._holder.client.get(f"/api/v1/products/{clean_code}", params=params)
        except httpx.HTTPError as exc:
            log_session_event(
                "rag", "exact_lookup_error", {"codigo_orig": clean_code, "error": str(exc)}, level="ERROR"
            )
            raise RagProductError(f"rag exact lookup failed for {clean_code!r}: {exc}") from exc
        if response.status_code == 404:
            return ()
        if response.status_code != 200:
            log_session_event(
                "rag",
                "exact_lookup_error",
                {"codigo_orig": clean_code, "status": response.status_code},
                level="WARNING",
            )
            raise RagProductError(
                f"rag exact lookup returned HTTP {response.status_code} for {clean_code!r}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RagProductError(f"rag exact lookup returned non-JSON payload: {exc}") from exc
        if not isinstance(data, list):
            raise RagProductError("rag exact lookup returned an invalid payload")
        mapped = tuple(self._map_exact_product(raw) for raw in data if isinstance(raw, dict))
        log_session_event(
            "rag",
            "exact_lookup_success",
            {"codigo_orig": clean_code, "matches": len(mapped)},
        )
        return mapped

    def ingest_catalog(
        self,
        *,
        filename: str,
        content: bytes,
        codigo_proveedor: str,
        nombre_proveedor: str,
        proveedor_id: str | None = None,
        documento_id: str | None = None,
        delete_scope: str = "proveedor",
        start_page: int = 1,
        max_pages: int | None = None,
        skip_pages: str | None = None,
        no_vision: bool = False,
        marca: str | None = None,
    ) -> str:
        """Upload a supplier catalog PDF for async ingestion (``sync=false``).

        ``POST /api/v1/catalogs/ingest-file`` with multipart data. The service
        accepts the upload with HTTP 202 and returns a ``JobStatusResponse``
        whose ``job_id`` tracks the background ingestion. The upload timeout is
        scaled by ``RAG_INGEST_TIMEOUT_FACTOR`` because PDFs are large; the
        ingestion itself is async server-side. Transport failures, timeouts,
        non-2xx statuses and payloads without a ``job_id`` raise
        ``RagProductError`` (never a raw httpx exception).

        Deletion scope (``delete_scope``): a supplier's catalog may be split
        across multiple PDF files. ``documento_id`` is the logical document
        identity DECLARED BY THE OPERATOR (e.g. ``"LISTA GENERAL"``) — never
        derived from filename or content.

        - ``delete_scope="proveedor"`` (default): full replace — the service
          deletes every previously indexed row for ``codigo_proveedor`` before
          inserting, wiping other files' products. Callers must warn the user.
        - ``delete_scope="documento"``: incremental — deletes only rows matching
          ``codigo_proveedor + documento_id`` so the remaining files of the
          same document survive the re-ingest.

        When ``documento_id`` is absent the service applies its own default
        ("LISTA GENERAL") for both scopes, so every indexed row ends up tagged
        (no NULL ``documento_id`` going forward); this client keeps sending
        ``documento_id`` only when the caller supplies one.

        Optional page-windowing / extraction knobs mirror the backend's form
        fields and follow the same omission pattern as ``proveedor_id``/
        ``documento_id``: they are only sent when non-default, so the
        multipart payload stays minimal.

        - ``start_page``: 1-indexed page where ingestion begins (omitted
          when 1).
        - ``max_pages``: cap on how many pages to process; ``None`` means
          no limit (omitted).
        - ``skip_pages``: pages/ranges to skip, e.g. ``"1-2,4"``. The
          backend parses this leniently and silently ignores invalid
          tokens (omitted when empty/whitespace).
        - ``no_vision``: disable multimodal extraction, text-only
          processing (omitted when ``False``).
        - ``marca``: force this brand on every extracted product
          (omitted when empty/whitespace).
        """
        if not filename or not content:
            raise ValueError("filename and content are required for catalog ingestion")
        data: dict[str, str] = {
            "codigo_proveedor": codigo_proveedor,
            "nombre_proveedor": nombre_proveedor,
            "sync": "false",
        }
        if proveedor_id:
            data["proveedor_id"] = proveedor_id
        clean_documento_id = (documento_id or "").strip()
        if clean_documento_id:
            data["documento_id"] = clean_documento_id
        if delete_scope and delete_scope != "proveedor":
            data["delete_scope"] = delete_scope
        if start_page and start_page != 1:
            data["start_page"] = str(start_page)
        if max_pages:
            data["max_pages"] = str(max_pages)
        clean_skip_pages = (skip_pages or "").strip()
        if clean_skip_pages:
            data["skip_pages"] = clean_skip_pages
        if no_vision:
            data["no_vision"] = "true"
        clean_marca = (marca or "").strip()
        if clean_marca:
            data["marca"] = clean_marca
        started = time.perf_counter()
        try:
            response = self._holder.client.post(
                RAG_CATALOG_INGEST_PATH,
                files={"file": (filename, content)},
                data=data,
                timeout=self.settings.rag_timeout_seconds * RAG_INGEST_TIMEOUT_FACTOR,
            )
        except httpx.HTTPError as exc:
            log_session_event(
                "rag",
                "catalog_ingest_error",
                {"filename": filename, "error": str(exc)},
                level="ERROR",
            )
            raise RagProductError(f"rag catalog ingest failed for {filename!r}: {exc}") from exc
        if response.status_code not in (200, 202):
            log_session_event(
                "rag",
                "catalog_ingest_error",
                {"filename": filename, "status": response.status_code},
                level="WARNING",
            )
            raise RagProductError(
                f"rag catalog ingest returned HTTP {response.status_code} for {filename!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RagProductError(f"rag catalog ingest returned non-JSON payload: {exc}") from exc
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise RagProductError("rag catalog ingest returned no job_id")
        log_session_event(
            "rag",
            "catalog_ingest_accepted",
            {
                "filename": filename,
                "job_id": job_id,
                "latency_sec": round(time.perf_counter() - started, 3),
            },
        )
        logger.info("rag catalog ingest job=%s filename=%r", job_id, filename)
        return job_id

    def get_job(self, job_id: str) -> RagJobStatus:
        """Fetch one ingestion job snapshot via ``GET /api/v1/jobs/{job_id}``.

        The typed ``RagJobStatus`` carries ``status`` plus the progress/result/
        error fields; a 404 (unknown job id) and any other non-200 status,
        transport failure or unparsable payload raise ``RagProductError``.
        """
        clean_job_id = str(job_id or "").strip()
        if not clean_job_id:
            raise ValueError("job_id is required to fetch job status")
        try:
            response = self._holder.client.get(RAG_JOB_STATUS_PATH.format(job_id=clean_job_id))
        except httpx.HTTPError as exc:
            log_session_event(
                "rag",
                "job_status_error",
                {"job_id": clean_job_id, "error": str(exc)},
                level="ERROR",
            )
            raise RagProductError(f"rag job status failed for {clean_job_id!r}: {exc}") from exc
        if response.status_code != 200:
            log_session_event(
                "rag",
                "job_status_error",
                {"job_id": clean_job_id, "status": response.status_code},
                level="WARNING",
            )
            raise RagProductError(
                f"rag job status returned HTTP {response.status_code} for {clean_job_id!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RagProductError(f"rag job status returned non-JSON payload: {exc}") from exc
        status_value = payload.get("status")
        if not isinstance(status_value, str) or not status_value.strip():
            raise RagProductError("rag job status returned no status")
        result = payload.get("result")
        return RagJobStatus(
            job_id=str(payload.get("job_id") or clean_job_id),
            status=status_value,
            progress_message=payload.get("progress_message"),
            result=result if isinstance(result, dict) else None,
            error=payload.get("error"),
        )

    def list_documents(self, codigo_proveedor: str) -> RagProviderDocuments:
        """List the indexed documents of ``codigo_proveedor``.

        ``GET /api/v1/catalogs/documents`` returns one entry per distinct
        ``documento_id`` (legacy rows with a NULL ``documento_id`` are
        excluded). Feeds the backoffice "Documento / lista" dropdown so the
        operator picks a known document — or types a brand-new one — instead
        of free-typing a typo'd ``documento_id`` that would orphan rows. An
        unknown provider answers 200 with an empty list and maps to an empty
        ``documents`` tuple (not an error); transport failures, non-200
        statuses and unparsable payloads raise ``RagProductError``.
        """
        clean_code = str(codigo_proveedor or "").strip()
        if not clean_code:
            raise ValueError("codigo_proveedor is required to list documents")
        try:
            response = self._holder.client.get(
                RAG_CATALOG_DOCUMENTS_PATH,
                params={"codigo_proveedor": clean_code},
            )
        except httpx.HTTPError as exc:
            log_session_event(
                "rag",
                "documents_error",
                {"codigo_proveedor": clean_code, "error": str(exc)},
                level="ERROR",
            )
            raise RagProductError(
                f"rag documents list failed for {clean_code!r}: {exc}"
            ) from exc
        if response.status_code != 200:
            log_session_event(
                "rag",
                "documents_error",
                {"codigo_proveedor": clean_code, "status": response.status_code},
                level="WARNING",
            )
            raise RagProductError(
                f"rag documents list returned HTTP {response.status_code} for {clean_code!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RagProductError(
                f"rag documents list returned non-JSON payload: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("documentos"), list):
            raise RagProductError("rag documents list returned an invalid payload")
        documents: list[RagDocumentSummary] = []
        for raw in payload["documentos"]:
            if not isinstance(raw, dict):
                continue
            documento_id = raw.get("documento_id")
            if not documento_id:
                continue
            try:
                total_productos = int(raw.get("total_productos") or 0)
            except (TypeError, ValueError):
                total_productos = 0
            documents.append(
                RagDocumentSummary(
                    documento_id=str(documento_id),
                    total_productos=total_productos,
                    ultimo_archivo=raw.get("ultimo_archivo"),
                    actualizado_en=raw.get("actualizado_en"),
                )
            )
        log_session_event(
            "rag",
            "documents_success",
            {"codigo_proveedor": clean_code, "documents": len(documents)},
        )
        logger.info("rag documents list provider=%r count=%d", clean_code, len(documents))
        return RagProviderDocuments(codigo_proveedor=clean_code, documents=tuple(documents))

    def _map_exact_product(self, raw: dict[str, Any]) -> RagProduct:
        """Map one product-route row into a typed ``RagProduct`` with provenance.

        The display name is not a first-class column of the RAG table — the
        service exposes ``nombre``/``descripcion`` parsed from ``text_content``.
        ``node_id`` travels on every row so ingestion can persist provenance
        and detect duplicates.
        """
        return RagProduct(
            sku=raw.get("codigo_orig") or raw.get("codigo") or "",
            name=raw.get("nombre") or raw.get("descripcion") or "Producto",
            provider=raw.get("nombre_proveedor"),
            brand=raw.get("marca"),
            price=raw.get("precio"),
            currency=raw.get("moneda"),
            source_file=raw.get("archivo_origen"),
            page=raw.get("pagina_origen"),
            codigo_proveedor=raw.get("codigo_proveedor") or None,
            node_id=raw.get("node_id") or None,
        )

    def _map_product(
        self, raw: dict[str, Any], fragment_to_chunk: dict[int, dict[str, Any]]
    ) -> RagProduct:
        """Map one ``productos[]`` row into a typed ``RagProduct``.

        SKU hygiene (ADR 2): prefer ``codigo_orig``, falling back to the
        ``codigo`` normalized against a duplicated ``{provider}-`` prefix.
        Provenance: the row's ``fragmento_id`` resolves ``node_id`` and the
        category fields from the matching ``context_chunks`` entry; the row's
        own values win when present. An unresolvable fragment leaves
        ``node_id`` as ``None`` (adoption fails closed on it).
        """
        codigo = raw.get("codigo") or ""
        provider = raw.get("codigo_proveedor") or ""
        fragment_id = _as_fragment_id(raw.get("fragmento_id"))
        chunk = fragment_to_chunk.get(fragment_id) if fragment_id is not None else None
        return RagProduct(
            sku=raw.get("codigo_orig") or normalize_rag_sku(codigo, provider),
            name=raw["nombre"],
            provider=raw.get("nombre_proveedor"),
            brand=raw.get("marca"),
            price=raw.get("precio"),
            currency=raw.get("moneda"),
            unit=raw.get("unidad_venta"),
            specs=raw.get("especificaciones"),
            source_file=raw.get("archivo_origen"),
            page=raw.get("pagina"),
            codigo_proveedor=raw.get("codigo_proveedor") or None,
            node_id=chunk.get("node_id") if chunk else None,
            fragment_id=fragment_id,
            categoria_padre=raw.get("categoria_padre")
            or (chunk.get("categoria_padre") if chunk else None),
            categoria=raw.get("categoria") or (chunk.get("categoria") if chunk else None),
            subcategoria=raw.get("subcategoria") or (chunk.get("subcategoria") if chunk else None),
        )
