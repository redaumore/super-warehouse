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
    DocumentLine,
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
            categoria_padre="Fijaciones",
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


def _provenance_response(product: dict, chunks: list[dict]) -> dict:
    response = _success_response(product)
    response["context_chunks"] = chunks
    return response


def test_rag_client_resolves_node_id_from_context_chunks():
    """El node_id del producto se resuelve desde context_chunks vía fragmento_id."""
    chunk = {
        "fragment_id": 1,
        "node_id": "node_prod_AMX-AT-5044",
        "codigo_producto": "AMX-AT-5044",
    }
    client = _client(
        lambda request: httpx.Response(
            200, json=_provenance_response(_product(fragmento_id=1), [chunk])
        )
    )

    products = client.query("tarugos")

    assert products[0].node_id == "node_prod_AMX-AT-5044"
    assert products[0].fragment_id == 1


def test_rag_client_unresolved_fragment_leaves_node_id_none():
    """Un fragmento sin chunk asociado deja node_id en None (provenance ausente)."""
    client = _client(
        lambda request: httpx.Response(
            200,
            json=_provenance_response(
                _product(fragmento_id=99), [{"fragment_id": 1, "node_id": "node_prod_otro"}]
            ),
        )
    )

    products = client.query("tarugos")

    assert products[0].node_id is None
    assert products[0].fragment_id == 99


def test_rag_client_fills_categories_from_context_chunk():
    """Las categorías ausentes en la fila se completan desde el chunk del contexto."""
    chunk = {
        "fragment_id": 1,
        "node_id": "node_prod_AMX-AT-5044",
        "categoria_padre": "Fijaciones",
        "categoria": "Tarugos",
        "subcategoria": "Plástico",
    }
    client = _client(
        lambda request: httpx.Response(
            200,
            json=_provenance_response(
                _product(fragmento_id=1, categoria_padre=None, categoria=None, subcategoria=None),
                [chunk],
            ),
        )
    )

    products = client.query("tarugos")

    assert products[0].categoria_padre == "Fijaciones"
    assert products[0].categoria == "Tarugos"
    assert products[0].subcategoria == "Plástico"


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


# ------------------------------------------------- document parse (W2 rag-doc)


def _parse_response(*lines: dict) -> dict:
    return {
        "document": {
            "lines": list(lines),
            "source_filename": "remito.jpg",
            "source_pages": 1,
        }
    }


def test_parse_document_maps_lines_and_sends_multipart(monkeypatch):
    """Un parse exitoso mapea líneas tipadas y envía multipart con proveedor."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        body = request.content.decode("utf-8", errors="replace")
        seen["has_filename"] = "remito.jpg" in body
        seen["has_provider"] = "AMX" in body
        return httpx.Response(
            200,
            json=_parse_response(
                {
                    "codigo_orig": "AT-5044",
                    "codigo": None,
                    "descripcion": "Tarugo Fischer 8mm",
                    "cantidad": 10,
                    "costo": 135.5,
                    "pagina": 1,
                },
                {
                    "codigo_orig": "AT-5045",
                    "codigo": None,
                    "descripcion": "Tarugo Fischer 10mm",
                    "cantidad": 4,
                    "costo": None,
                    "pagina": 1,
                },
            ),
        )

    client = _client(handler)
    lines = client.parse_document(
        filename="remito.jpg", content=b"fake-image", codigo_proveedor="AMX"
    )

    assert seen["url"] == "http://rag.test/api/v1/ingest/parse"
    assert seen["content_type"].startswith("multipart/form-data")
    assert seen["has_filename"] and seen["has_provider"]
    assert lines == (
        DocumentLine(
            codigo_orig="AT-5044",
            codigo=None,
            descripcion="Tarugo Fischer 8mm",
            cantidad=10,
            costo=135.5,
            pagina=1,
        ),
        DocumentLine(
            codigo_orig="AT-5045",
            codigo=None,
            descripcion="Tarugo Fischer 10mm",
            cantidad=4,
            costo=None,
            pagina=1,
        ),
    )


def test_parse_document_transport_failure_raises_domain_error():
    """Un error de conexión al parsear se convierte en RagProductError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(RagProductError, match="connection refused"):
        client.parse_document(filename="remito.jpg", content=b"x", codigo_proveedor="AMX")


def test_parse_document_timeout_raises_domain_error():
    """Un timeout del parse se convierte en RagProductError, nunca transport crudo."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    client = _client(handler)
    with pytest.raises(RagProductError, match="timed out"):
        client.parse_document(filename="remito.jpg", content=b"x", codigo_proveedor="AMX")


def test_parse_document_http_error_raises_domain_error():
    """Un HTTP 500 del parse se convierte en RagProductError."""
    client = _client(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(RagProductError, match="HTTP 500"):
        client.parse_document(filename="remito.jpg", content=b"x", codigo_proveedor="AMX")


def test_parse_document_requires_filename_and_content():
    """Faltar filename/content es un error de uso, no un error de transporte."""
    client = _client(lambda request: httpx.Response(200, json=_parse_response()))
    with pytest.raises(ValueError):
        client.parse_document(filename="", content=b"x", codigo_proveedor="AMX")
    with pytest.raises(ValueError):
        client.parse_document(filename="remito.jpg", content=b"", codigo_proveedor="AMX")


# ------------------------------------------------ exact lookup (W2 rag-product-query)


def _exact_row(**fields: object) -> dict:
    base = {
        "codigo_orig": "AT-5044",
        "codigo": "AMX-AT-5044",
        "codigo_proveedor": "AMX",
        "nombre_proveedor": "AMX",
        "nombre": "Tarugo Fischer 8mm",
        "marca": "Fischer",
        "precio": 135.5,
        "moneda": "ARS",
        "pagina_origen": 12,
        "archivo_origen": "catalogo-2024.pdf",
        "node_id": "node_prod_AMX-AT-5044",
    }
    base.update(fields)
    return base


def test_exact_lookup_returns_all_matches_with_node_id():
    """Un lookup exacto devuelve TODAS las coincidencias, cada una con node_id."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                _exact_row(),
                _exact_row(node_id="node_prod_AMX-AT-5044-bis", precio=140.0),
            ],
        )

    client = _client(handler)
    matches = client.exact_lookup("AT-5044", "AMX")

    assert seen["url"] == "http://rag.test/api/v1/products/AT-5044?codigo_proveedor=AMX"
    assert len(matches) == 2
    assert matches[0].node_id == "node_prod_AMX-AT-5044"
    assert matches[0].sku == "AT-5044"
    assert matches[0].name == "Tarugo Fischer 8mm"
    assert matches[1].node_id == "node_prod_AMX-AT-5044-bis"
    assert matches[1].price == 140.0


def test_exact_lookup_404_returns_empty_tuple():
    """Un 404 (sin coincidencias) se mapea a tupla vacía, no a error."""
    client = _client(lambda request: httpx.Response(404, json={"detail": "not found"}))

    assert client.exact_lookup("UNKNOWN", "AMX") == ()


def test_exact_lookup_transport_failure_raises_domain_error():
    """Un error de conexión se convierte en RagProductError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(RagProductError, match="connection refused"):
        client.exact_lookup("AT-5044", "AMX")


def test_exact_lookup_timeout_raises_domain_error():
    """Un timeout del lookup exacto se convierte en RagProductError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    client = _client(handler)
    with pytest.raises(RagProductError, match="timed out"):
        client.exact_lookup("AT-5044", "AMX")


def test_rag_client_timeout_bounded_by_settings():
    """El timeout del cliente proviene de rag_timeout_seconds (src/config.py:73)."""
    client = _client(lambda request: httpx.Response(200, json=[]), rag_timeout_seconds=3.5)
    assert client._holder._timeout == 3.5


# --------------------------------------- catalog ingestion + job status (async)


def _job_payload(**overrides) -> dict:
    base = {
        "job_id": "job-123",
        "status": "PENDING",
        "created_at": "2026-01-01T00:00:00Z",
        "source_document": "/uploads/catalogo.pdf",
        "table_name": "catalogo_productos_rag",
        "codigo_proveedor": "MSA",
    }
    base.update(overrides)
    return base


def test_ingest_catalog_202_returns_job_id_and_sends_multipart():
    """Un 202 con job_id devuelve el id; el multipart lleva proveedor y sync=false."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        body = request.content.decode("utf-8", errors="replace")
        seen["has_filename"] = "catalogo.pdf" in body
        seen["has_codigo"] = "MSA" in body
        seen["has_nombre"] = "Mayorista SA" in body
        seen["has_sync_false"] = 'name="sync"\r\n\r\nfalse' in body
        seen["has_proveedor_id"] = 'name="proveedor_id"\r\n\r\n1' in body
        return httpx.Response(202, json=_job_payload())

    client = _client(handler)
    job_id = client.ingest_catalog(
        filename="catalogo.pdf",
        content=b"%PDF-fake",
        codigo_proveedor="MSA",
        nombre_proveedor="Mayorista SA",
        proveedor_id="1",
    )

    assert job_id == "job-123"
    assert seen["url"] == "http://rag.test/api/v1/catalogs/ingest-file"
    assert seen["content_type"].startswith("multipart/form-data")
    assert seen["has_filename"] and seen["has_codigo"] and seen["has_nombre"]
    assert seen["has_sync_false"] and seen["has_proveedor_id"]


def test_ingest_catalog_omits_proveedor_id_when_absent():
    """Sin proveedor_id el form no incluye el campo (opcional en el servicio)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", errors="replace")
        seen["has_proveedor_id"] = "proveedor_id" in body
        return httpx.Response(202, json=_job_payload())

    client = _client(handler)
    job_id = client.ingest_catalog(
        filename="catalogo.pdf",
        content=b"%PDF-fake",
        codigo_proveedor="MSA",
        nombre_proveedor="Mayorista SA",
    )
    assert job_id == "job-123"
    assert not seen["has_proveedor_id"]


def test_ingest_catalog_http_error_raises_domain_error():
    """Un HTTP 500 del ingest-file se convierte en RagProductError."""
    client = _client(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(RagProductError, match="HTTP 500"):
        client.ingest_catalog(
            filename="catalogo.pdf",
            content=b"x",
            codigo_proveedor="MSA",
            nombre_proveedor="Mayorista SA",
        )


def test_ingest_catalog_connect_error_raises_domain_error():
    """Un error de conexión al subir el PDF se convierte en RagProductError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(RagProductError, match="connection refused"):
        client.ingest_catalog(
            filename="catalogo.pdf",
            content=b"x",
            codigo_proveedor="MSA",
            nombre_proveedor="Mayorista SA",
        )


def test_ingest_catalog_requires_filename_and_content():
    """Faltar filename/content es un error de uso, no de transporte."""
    client = _client(lambda request: httpx.Response(202, json=_job_payload()))
    with pytest.raises(ValueError):
        client.ingest_catalog(
            filename="", content=b"x", codigo_proveedor="MSA", nombre_proveedor="Mayorista SA"
        )
    with pytest.raises(ValueError):
        client.ingest_catalog(
            filename="catalogo.pdf",
            content=b"",
            codigo_proveedor="MSA",
            nombre_proveedor="Mayorista SA",
        )


def test_ingest_catalog_missing_job_id_raises_domain_error():
    """Un 202 sin job_id en el payload se convierte en RagProductError."""
    client = _client(lambda request: httpx.Response(202, json={"status": "PENDING"}))
    with pytest.raises(RagProductError, match="no job_id"):
        client.ingest_catalog(
            filename="catalogo.pdf",
            content=b"x",
            codigo_proveedor="MSA",
            nombre_proveedor="Mayorista SA",
        )


def test_get_job_200_maps_typed_status():
    """Un 200 mapea el snapshot del job a RagJobStatus tipado con result y error."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://rag.test/api/v1/jobs/job-123"
        return httpx.Response(
            200,
            json=_job_payload(
                status="COMPLETED",
                progress_message="Ingesta finalizada con éxito",
                result={"total_productos": 42},
            ),
        )

    client = _client(handler)
    job = client.get_job("job-123")
    assert job.job_id == "job-123"
    assert job.status == "COMPLETED"
    assert job.progress_message == "Ingesta finalizada con éxito"
    assert job.result == {"total_productos": 42}
    assert job.error is None


def test_get_job_failed_maps_error_detail():
    """Un job FAILED expone el detalle de error del servicio."""
    client = _client(
        lambda request: httpx.Response(200, json=_job_payload(status="FAILED", error="OCR explode"))
    )
    job = client.get_job("job-123")
    assert job.status == "FAILED"
    assert job.error == "OCR explode"


def test_get_job_requires_job_id():
    """Un job_id vacío es un error de uso, no de transporte."""
    client = _client(lambda request: httpx.Response(200, json=_job_payload()))
    with pytest.raises(ValueError):
        client.get_job("   ")


def test_get_job_unknown_id_and_transport_errors_raise_domain_error():
    """Un 404 (job desconocido) y un fallo de transporte son RagProductError."""
    not_found = _client(lambda request: httpx.Response(404, json={"detail": "no such job"}))
    with pytest.raises(RagProductError, match="HTTP 404"):
        not_found.get_job("missing")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = _client(handler)
    with pytest.raises(RagProductError, match="connection refused"):
        transport.get_job("job-123")


def test_get_job_missing_status_raises_domain_error():
    """Un 200 sin status se convierte en RagProductError."""
    client = _client(lambda request: httpx.Response(200, json={"job_id": "job-123"}))
    with pytest.raises(RagProductError, match="no status"):
        client.get_job("job-123")
