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

logger = logging.getLogger(__name__)


def normalize_rag_sku(codigo: str, provider: str) -> str:
    """Collapse a duplicated ``{provider}-`` prefix in a RAG ``codigo``.

    The RAG concatenates ``codigo_proveedor`` + ``codigo_orig`` and has been
    observed emitting double prefixes (``AMX-AMX-AT-5044``). Display must not
    trust the raw ``codigo``: this collapses every repeated leading
    ``{provider}-`` pair down to a single prefix and leaves already-clean codes
    untouched (no-double case).
    """
    if not provider:
        return codigo
    prefix = f"{provider}-"
    double = prefix * 2
    while codigo.startswith(double):
        codigo = codigo[len(prefix) :]
    return codigo


@dataclass(frozen=True)
class RagProduct:
    """One typed product result from ``structured_json.productos[]``."""

    sku: str
    name: str
    provider: str | None = None
    brand: str | None = None
    price: float | None = None
    currency: str | None = None
    unit: str | None = None
    specs: str | None = None
    source_file: str | None = None
    page: int | None = None
    codigo_proveedor: str | None = None
    node_id: str | None = None
    fragment_id: int | None = None
    categoria_padre: str | None = None
    categoria: str | None = None
    subcategoria: str | None = None


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
class RagPrice:
    """Price snapshot returned by the RAG product lookup endpoint."""

    price: float | None
    currency: str | None


class RagProductError(Exception):
    """The RAG query failed (transport, status, or unparsable payload)."""


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
            categoria_padre=raw.get("categoria_padre") or (
                chunk.get("categoria_padre") if chunk else None
            ),
            categoria=raw.get("categoria") or (chunk.get("categoria") if chunk else None),
            subcategoria=raw.get("subcategoria") or (chunk.get("subcategoria") if chunk else None),
        )
