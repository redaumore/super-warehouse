"""Domain-owned supplier-catalog RAG vocabulary (types, errors, SKU hygiene).

The supplier-catalog RAG results are domain vocabulary: L1 modules
(``agents.product_search``, ``supplier.rag_searcher``, ``sourcing.draft_order``)
must be able to speak about RAG products, prices and failures without importing
the L2 ``httpx`` adapter. This module owns those definitions; the adapter in
``src/integrations/rag.py`` imports them from here (the legal L2 -> L1
direction) and re-exports them so existing L2/L3 consumers and tests keep
working with object identity preserved (``src.integrations.rag.RagProduct is
src.supplier.rag_catalog.RagProduct``).
"""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class RagPrice:
    """Price snapshot returned by the RAG product lookup endpoint."""

    price: float | None
    currency: str | None


class RagProductError(Exception):
    """The RAG query failed (transport, status, or unparsable payload)."""
