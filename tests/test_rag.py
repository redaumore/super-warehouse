"""Supplier-catalog RAG client tests (W1 of rag-product-query).

Covers SKU hygiene (``normalize_rag_sku``) as parametrized pure-function cases
and the HTTP boundary with ``httpx.MockTransport``: successful product mapping,
refusal/empty handling, and transport failures surfacing as domain errors
(``RagProductError``), never as raw ``httpx`` exceptions. No network, no DB.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.config import Settings
from src.integrations.rag import (
    RagPrice,
    RagProduct,
    RagProductClient,
    RagProductError,
    RagProductNotConfigured,
    normalize_rag_sku,
)


def _settings(**overrides) -> Settings:
    base = {
        "rag_base_url": "http://rag.test",
        "rag_table_name": "tabla_prueba",
        "rag_top_n": 5,
        "rag_threshold": 0.6,
        "rag_model": "gpt-4o-mini",
        "rag_timeout_seconds": 2.0,
    }
    base.update(overrides)
    return Settings(**base)


def _client(handler, **settings_overrides) -> RagProductClient:
    transport = httpx.MockTransport(handler)
    return RagProductClient(transport=transport, settings=_settings(**settings_overrides))


def _product(**fields) -> dict:
    base = {
        "codigo": "AMX-AT-5044",
        "codigo_orig": "AT-5044",
        "codigo_proveedor": "AMX",
        "nombre_proveedor": "AMX",
        "marca": "Fischer",
        "nombre": "Tarugo Fischer 8mm",
        "categoria_padre": "Fijaciones",
        "precio": 135.5,
        "moneda": "ARS",
        "unidad_venta": "bolsa",
        "especificaciones": "plástico, 8mm",
        "archivo_origen": "catalogo-2024.pdf",
        "pagina": 12,
    }
    base.update(fields)
    return base


def _success_response(*products: dict, is_refusal: bool = False) -> dict:
    return {
        "query": "tarugos",
        "response_text": "ok",
        "is_refusal": is_refusal,
        "status": "REFUSAL_GROUNDED" if is_refusal else "SUCCESS",
        "structured_json": {
            "respuesta_narrativa": "narrative",
            "consulta_respondida": "tarugos",
            "productos": list(products),
        },
    }


@pytest.mark.parametrize(
    ("codigo", "provider", "expected"),
    [
        ("AMX-AMX-AT-5044", "AMX", "AMX-AT-5044"),
        ("AMX-AT-5044", "AMX", "AMX-AT-5044"),
        ("AMX-AMX-AMX-AT-5044", "AMX", "AMX-AT-5044"),
        ("AT-5044", "AMX", "AT-5044"),
        ("AMX-AMX-AT-5044", "", "AMX-AMX-AT-5044"),
    ],
    ids=[
        "double-prefix-collapsed",
        "no-double-untouched",
        "triple-prefix-collapsed",
        "no-provider-prefix-untouched",
        "empty-provider-untouched",
    ],
)
def test_normalize_rag_sku(codigo: str, provider: str, expected: str):
    """El SKU RAG con prefijo duplicado se normaliza a una sola forma."""
    assert normalize_rag_sku(codigo, provider) == expected


def test_rag_client_query_maps_products_and_sends_structured_json():
    """Un query exitoso mapea productos tipados y pide structured_json=true."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_success_response(_product()))

    client = _client(handler)
    products = client.query("tarugos")

    assert seen["url"] == "http://rag.test/api/v1/query"
    assert seen["payload"] == {
        "query": "tarugos",
        "table_name": "tabla_prueba",
        "top_n": 5,
        "threshold": 0.6,
        "structured_json": True,
        "model": "gpt-4o-mini",
    }
    assert products == (
        RagProduct(
            sku="AT-5044",
            name="Tarugo Fischer 8mm",
            provider="AMX",
            brand="Fischer",
            price=135.5,
            currency="ARS",
            unit="bolsa",
            specs="plástico, 8mm",
            source_file="catalogo-2024.pdf",
            page=12,
            codigo_proveedor="AMX",
        ),
    )


def test_rag_client_refusal_returns_empty_tuple():
    """Un is_refusal=true se traduce a lista vacía (no encontrado en catálogos)."""
    client = _client(lambda request: httpx.Response(200, json=_success_response(is_refusal=True)))

    assert client.query("tarugos") == ()


def test_rag_client_empty_products_returns_empty_tuple():
    """Un SUCCESS sin productos devuelve lista vacía, no un error."""
    client = _client(lambda request: httpx.Response(200, json=_success_response()))

    assert client.query("tarugos") == ()


def test_rag_client_skips_products_without_name():
    """Los productos sin nombre se omiten del resultado tipado."""
    client = _client(
        lambda request: httpx.Response(
            200, json=_success_response(_product(nombre=None), _product())
        )
    )

    products = client.query("tarugos")

    assert len(products) == 1
    assert products[0].name == "Tarugo Fischer 8mm"


def test_rag_client_prefers_codigo_orig_over_normalized_codigo():
    """El codigo_orig gana; el codigo normalizado es solo el fallback."""
    client = _client(
        lambda request: httpx.Response(
            200,
            json=_success_response(_product(codigo="AMX-AMX-AT-5044", codigo_orig="AMX-AT-5044")),
        )
    )

    products = client.query("tarugos")

    assert products[0].sku == "AMX-AT-5044"


def test_rag_client_normalizes_double_prefix_codigo():
    """Sin codigo_orig, el codigo con doble prefijo se normaliza al mostrarlo."""
    client = _client(
        lambda request: httpx.Response(
            200,
            json=_success_response(_product(codigo="AMX-AMX-AT-5044", codigo_orig=None)),
        )
    )

    products = client.query("tarugos")

    assert products[0].sku == "AMX-AT-5044"


def test_rag_client_connect_error_raises_domain_error():
    """Un error de conexión se convierte en RagProductError, nunca transport crudo."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(RagProductError, match="connection refused"):
        client.query("tarugos")


def test_rag_client_read_timeout_raises_domain_error():
    """Un timeout de lectura se convierte en RagProductError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    client = _client(handler)
    with pytest.raises(RagProductError, match="timed out"):
        client.query("tarugos")


def test_rag_client_http_500_raises_domain_error():
    """Un HTTP 500 del servicio se convierte en RagProductError."""
    client = _client(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(RagProductError, match="HTTP 500"):
        client.query("tarugos")


def test_rag_client_malformed_json_raises_domain_error():
    """Un payload 200 no-JSON se convierte en RagProductError."""
    client = _client(lambda request: httpx.Response(200, text="not json"))

    with pytest.raises(RagProductError, match="non-JSON"):
        client.query("tarugos")


def test_rag_client_without_base_url_raises_not_configured():
    """Sin RAG_BASE_URL el cliente lanza RagProductNotConfigured al usarse."""
    client = _client(lambda request: httpx.Response(200, json={}), rag_base_url="")

    with pytest.raises(RagProductNotConfigured):
        client.query("tarugos")


def test_rag_client_injected_client_is_used_directly():
    """Un httpx.Client inyectado se usa tal cual, sin construir otro desde settings."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_success_response(_product()))

    client = RagProductClient(
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://inj"),
        settings=_settings(),
    )

    client.query("tarugos")

    assert seen == ["http://inj/api/v1/query"]


def test_price_lookup_200_maps_price_and_supplier_query_parameter():
    """A successful price lookup returns the offer and forwards the supplier code."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"codigo": "AT-5044", "precio": 135.5, "moneda": "usd"})

    client = _client(handler)

    assert client.price_lookup("AT-5044", codigo_proveedor="AMX") == RagPrice(135.5, "USD")
    assert seen["url"] == "http://rag.test/api/v1/products/AT-5044?codigo_proveedor=AMX"


def test_price_lookup_404_returns_none():
    """A missing supplier product is a normal lookup miss."""
    client = _client(lambda request: httpx.Response(404, json={"detail": "not found"}))

    assert client.price_lookup("UNKNOWN", codigo_proveedor="AMX") is None


def test_price_lookup_transport_and_server_errors_raise_domain_error():
    """Transport and server failures never leak raw HTTP exceptions."""
    transport_client = _client(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("connection refused"))
    )
    with pytest.raises(RagProductError, match="connection refused"):
        transport_client.price_lookup("AT-5044")

    server_client = _client(lambda request: httpx.Response(503, text="unavailable"))
    with pytest.raises(RagProductError, match="HTTP 503"):
        server_client.price_lookup("AT-5044")
