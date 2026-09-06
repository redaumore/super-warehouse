"""Endpoint tests for the document-ingestion API (W1 of rag-document-ingestion).

New pattern for this service: TestClient + monkeypatched OpenAI, NO live DB.
The parse endpoint's parser is patched via ``_get_parser`` with a fake OpenAI
client that returns canned structured output; the DB-backed lookup endpoints
patch ``_fetch_exact_matches`` so nothing touches Postgres. Every scenario also
asserts the no-writes guarantee (uploads dir untouched).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.v1.endpoints import ingestion as ingestion_ep
from app.core.ingestion.document_parser import ReceiptLine, ReceiptPageResult
from app.main import app

client = TestClient(app)


# --------------------------------------------------------------------------- helpers


def _fake_parse_client(lines: list[dict]) -> SimpleNamespace:
    """OpenAI-shaped fake returning a canned ``ReceiptPageResult``."""

    def parse(*, model: str, messages: list, response_format):  # noqa: ARG001
        parsed = ReceiptPageResult(lineas=[ReceiptLine(**line) for line in lines])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )

    return SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))))


def _patch_parser(monkeypatch: pytest.MonkeyPatch, lines: list[dict]) -> None:
    """Point the endpoint's parser factory at a fake client returning ``lines``."""
    fake = _fake_parse_client(lines)

    def build_parser():
        parser = SimpleNamespace()
        parser.parse = lambda content, filename: _parsed_from_fake(fake, content, filename)  # noqa: ARG005
        return parser

    def _parsed_from_fake(fake_client, content: bytes, filename: str) -> object:
        page = fake_client.beta.chat.completions.parse(
            model="gpt-5.6-luna", messages=[], response_format=ReceiptPageResult
        )
        parsed = page.choices[0].message.parsed
        from app.api.schemas.ingest import DocumentLine, ParsedDocument

        return ParsedDocument(
            lines=[
                DocumentLine(
                    codigo_orig=line.codigo_orig,
                    codigo=line.codigo,
                    descripcion=line.descripcion,
                    cantidad=line.cantidad,
                    costo=line.costo,
                    pagina=1,
                )
                for line in parsed.lineas
            ],
            source_filename=filename,
            source_pages=1,
        )

    monkeypatch.setattr(ingestion_ep, "_get_parser", build_parser)


def _patch_parser_failure(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Make the parser factory raise ``exc`` on any document."""

    def build_parser():
        parser = SimpleNamespace()
        parser.parse = lambda content, filename: (_ for _ in ()).throw(exc)  # noqa: ARG005
        return parser

    monkeypatch.setattr(ingestion_ep, "_get_parser", build_parser)


def _uploads_count() -> int:
    import os

    from app.config import settings

    return len(os.listdir(settings.UPLOADS_DIR))


def _patch_exact(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> dict:
    """Stub ``_fetch_exact_matches`` recording the scoping args it received."""
    seen: dict = {}

    def fetch(codigo: str, codigo_proveedor: str | None, table_name: str) -> list[dict]:
        seen["codigo"] = codigo
        seen["codigo_proveedor"] = codigo_proveedor
        seen["table_name"] = table_name
        return rows

    monkeypatch.setattr(ingestion_ep, "_fetch_exact_matches", fetch)
    return seen


def _row(**overrides: object) -> dict:
    base = {
        "node_id": "node_prod_AMX-AT-5044",
        "codigo_producto": "AMX-AT-5044",
        "codigo_orig": "AT-5044",
        "marca": "Fischer",
        "categoria": "Tarugos",
        "subcategoria": "Plástico",
        "nombre_proveedor": "AMX",
        "codigo_proveedor": "AMX",
        "precio": 135.5,
        "moneda": "ARS",
        "pagina_origen": 12,
        "text_content": (
            "archivo_origen: catalogo-2024.pdf\n"
            "codigo_proveedor: AMX\n"
            "codigo_orig: AT-5044\n"
            "nombre: Tarugo Fischer 8mm\n"
            "descripcion: Tarugo de nylon 8mm\n"
            "precio: 135.5\n"
        ),
        "metadata": {"archivo_origen": "catalogo-2024.pdf"},
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------- parse endpoint


def test_ingest_parse_success_returns_lines_and_writes_nothing(monkeypatch: pytest.MonkeyPatch):
    """[rag-doc R2] Parse success: structured lines + source metadata, zero writes."""
    _patch_parser(
        monkeypatch,
        [{"codigo_orig": "AT-5044", "descripcion": "Tarugo Fischer 8mm", "cantidad": 10, "costo": 135.5}],
    )
    before = _uploads_count()
    response = client.post(
        "/api/v1/ingest/parse",
        files={"file": ("remito.jpg", b"fake-image-bytes", "image/jpeg")},
        data={"codigo_proveedor": "AMX"},
    )
    assert response.status_code == 200
    data = response.json()
    doc = data["document"]
    assert doc["source_filename"] == "remito.jpg"
    assert doc["source_pages"] == 1
    assert doc["lines"] == [
        {
            "codigo_orig": "AT-5044",
            "codigo": None,
            "descripcion": "Tarugo Fischer 8mm",
            "cantidad": 10,
            "costo": 135.5,
            "pagina": 1,
        }
    ]
    assert _uploads_count() == before  # never persisted


def test_ingest_parse_failure_returns_structured_error_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    """[rag-doc R2] Parse failure: structured error, zero writes."""
    _patch_parser_failure(monkeypatch, ValueError("no structured result"))
    before = _uploads_count()
    response = client.post(
        "/api/v1/ingest/parse",
        files={"file": ("remito.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data={"codigo_proveedor": "AMX"},
    )
    assert response.status_code == 422
    assert "parse failed" in response.json()["detail"]
    assert _uploads_count() == before


def test_ingest_parse_transport_failure_is_structured_error(monkeypatch: pytest.MonkeyPatch):
    """An OpenAI transport failure becomes a structured 500, never a crash."""
    _patch_parser_failure(monkeypatch, RuntimeError("openai connection refused"))
    response = client.post(
        "/api/v1/ingest/parse",
        files={"file": ("remito.jpg", b"fake", "image/jpeg")},
        data={"codigo_proveedor": "AMX"},
    )
    assert response.status_code == 500
    assert "parse error" in response.json()["detail"]


# --------------------------------------------------------------- exact lookup


def test_catalog_exact_scopes_to_supplier_and_returns_all_matches(
    monkeypatch: pytest.MonkeyPatch,
):
    """[rag-product-query R2] Exact lookup is scoped and returns ALL rows + node_id."""
    seen = _patch_exact(
        monkeypatch,
        [_row(), _row(node_id="node_prod_AMX-AT-5044-bis", precio=140.0)],
    )
    response = client.get("/api/v1/catalog/exact", params={"codigo_orig": "at-5044", "codigo_proveedor": "AMX"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["matches"]) == 2
    assert body["matches"][0]["node_id"] == "node_prod_AMX-AT-5044"
    assert body["matches"][0]["nombre"] == "Tarugo Fischer 8mm"
    assert body["matches"][0]["descripcion"] == "Tarugo de nylon 8mm"
    assert body["matches"][0]["archivo_origen"] == "catalogo-2024.pdf"
    assert body["matches"][1]["precio"] == 140.0
    assert seen["codigo"] == "at-5044"
    assert seen["codigo_proveedor"] == "AMX"


def test_exact_lookup_sql_normalizes_and_scopes():
    """The exact SQL uses UPPER(TRIM(codigo_orig)) and a supplier scope."""
    sql_scoped = ingestion_ep._exact_lookup_sql("catalogo_productos_rag", scoped=True).as_string(None)
    assert "UPPER(TRIM(codigo_orig)) = UPPER(TRIM(%s))" in sql_scoped
    assert "codigo_proveedor = %s" in sql_scoped
    assert "catalogo_productos_rag" in sql_scoped
    sql_unscoped = ingestion_ep._exact_lookup_sql("catalogo_productos_rag", scoped=False).as_string(None)
    assert "codigo_proveedor = %s" not in sql_unscoped


# -------------------------------------------------------------- products/{sku}


def test_products_sku_single_row(monkeypatch: pytest.MonkeyPatch):
    """[rag-product-query R2] Price lookup: 200 with a single-row payload."""
    seen = _patch_exact(monkeypatch, [_row()])
    response = client.get("/api/v1/products/AT-5044", params={"codigo_proveedor": "AMX"})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["codigo_orig"] == "AT-5044"
    assert body[0]["node_id"] == "node_prod_AMX-AT-5044"
    assert seen["codigo_proveedor"] == "AMX"


def test_products_sku_404_when_no_match(monkeypatch: pytest.MonkeyPatch):
    """[rag-product-query R2] Price lookup miss: 404, which the client maps to None."""
    _patch_exact(monkeypatch, [])
    response = client.get("/api/v1/products/UNKNOWN", params={"codigo_proveedor": "AMX"})
    assert response.status_code == 404


def test_products_sku_all_matches_with_node_id(monkeypatch: pytest.MonkeyPatch):
    """[rag-product-query R2] Ingestion resolution: array of ALL matches with node_id."""
    _patch_exact(
        monkeypatch,
        [_row(node_id="node_prod_1"), _row(node_id="node_prod_2")],
    )
    response = client.get("/api/v1/products/AT-5044", params={"codigo_proveedor": "AMX"})
    assert response.status_code == 200
    body = response.json()
    assert [m["node_id"] for m in body] == ["node_prod_1", "node_prod_2"]


def test_products_sku_unscoped_when_no_supplier(monkeypatch: pytest.MonkeyPatch):
    """Price lookup without supplier code stays unscoped (SKU-only match)."""
    seen = _patch_exact(monkeypatch, [_row()])
    response = client.get("/api/v1/products/AT-5044")
    assert response.status_code == 200
    assert seen["codigo_proveedor"] is None