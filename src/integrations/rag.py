"""Supplier-catalog RAG integration (rag-product-query change).

A synchronous ``httpx`` client for the sibling ``fase-0-pdf-parsing`` service
(``POST /api/v1/query`` with ``structured_json=true``). The transport is
injectable for tests (``httpx.MockTransport``) and the client is built lazily
from ``Settings`` on first use — mirroring the ``OpenAIResponder`` pattern in
``integrations/openai.py``.

The service maps ``structured_json.productos[]`` into typed ``RagProduct``
results carrying code, name, provider, brand, price, specs and the source
page/PDF. Failures surface as domain errors (``RagProductError``), never as raw
transport exceptions: a timeout, a connection error or an HTTP 500 all become
``RagProductError`` so the caller can answer with an honest unavailability
notice instead of a wrong "not in stock" claim.

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
            raise RagProductError(f"rag query failed for {text!r}: {exc}") from exc
        latency = time.perf_counter() - started
        if response.status_code != 200:
            logger.warning("rag query status=%s latency=%.1fs", response.status_code, latency)
            raise RagProductError(f"rag query returned HTTP {response.status_code} for {text!r}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RagProductError(f"rag query returned non-JSON payload: {exc}") from exc
        if data.get("is_refusal"):
            logger.info("rag refusal for query=%r latency=%.1fs", text, latency)
            return ()
        structured = data.get("structured_json") or {}
        products = structured.get("productos") or []
        mapped = tuple(self._map_product(product) for product in products if product.get("nombre"))
        logger.info("rag query=%r products=%d latency=%.1fs", text, len(mapped), latency)
        return mapped

    def _map_product(self, raw: dict[str, Any]) -> RagProduct:
        """Map one ``productos[]`` row into a typed ``RagProduct``.

        SKU hygiene (ADR 2): prefer ``codigo_orig``, falling back to the
        ``codigo`` normalized against a duplicated ``{provider}-`` prefix.
        """
        codigo = raw.get("codigo") or ""
        provider = raw.get("codigo_proveedor") or ""
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
        )
