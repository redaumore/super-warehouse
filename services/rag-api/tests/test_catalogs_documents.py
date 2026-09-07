"""Endpoint tests for GET /api/v1/catalogs/documents (dropdown de documentos).

Patrón igual a tests/test_ingestion.py: TestClient + monkeypatch, SIN base de
datos real. Se parchea ``PgVectorManager.list_documents`` en el módulo del
endpoint para cubrir: mapeo de resúmenes tipados, proveedor desconocido (200
con lista vacía, no 404), validación del query param y fallo de la consulta.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.v1.endpoints import catalogs as catalogs_ep
from app.main import app

client = TestClient(app)


# --------------------------------------------------------------------------- helpers


def _patch_list_documents(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict]
) -> None:
    """Apunta ``PgVectorManager.list_documents`` a filas canned (sin Postgres)."""

    def fake_list_documents(self, codigo_proveedor: str) -> list[dict]:  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(catalogs_ep.PgVectorManager, "list_documents", fake_list_documents)


# --------------------------------------------------------------------------- happy path


def test_documents_endpoint_returns_typed_summaries(monkeypatch):
    """El endpoint mapea las filas del store a resúmenes tipados por documento."""
    _patch_list_documents(
        monkeypatch,
        [
            {
                "documento_id": "LISTA GENERAL",
                "total_productos": 42,
                "ultimo_archivo": "catalogo-mayorista.pdf",
                "actualizado_en": "2026-01-01T00:00:00+00:00",
            },
            {
                "documento_id": "OFERTAS",
                "total_productos": 7,
                "ultimo_archivo": None,
                "actualizado_en": None,
            },
        ],
    )
    response = client.get(
        "/api/v1/catalogs/documents", params={"codigo_proveedor": "MSA"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["codigo_proveedor"] == "MSA"
    assert data["documentos"] == [
        {
            "documento_id": "LISTA GENERAL",
            "total_productos": 42,
            "ultimo_archivo": "catalogo-mayorista.pdf",
            "actualizado_en": "2026-01-01T00:00:00+00:00",
        },
        {
            "documento_id": "OFERTAS",
            "total_productos": 7,
            "ultimo_archivo": None,
            "actualizado_en": None,
        },
    ]


def test_documents_endpoint_unknown_provider_returns_empty_list(monkeypatch):
    """Proveedor desconocido o sin documentos → 200 con lista vacía (no 404)."""
    _patch_list_documents(monkeypatch, [])
    response = client.get(
        "/api/v1/catalogs/documents", params={"codigo_proveedor": "ZZZ"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["codigo_proveedor"] == "ZZZ"
    assert data["documentos"] == []


# --------------------------------------------------------------------------- validación


def test_documents_endpoint_requires_codigo_proveedor(monkeypatch):
    """El query param codigo_proveedor es obligatorio (min_length=1 → 422)."""
    _patch_list_documents(monkeypatch, [])
    assert client.get("/api/v1/catalogs/documents").status_code == 422


# --------------------------------------------------------------------------- error path


def test_documents_endpoint_maps_query_failure_to_500(monkeypatch):
    """Un fallo de la consulta se mapea a 500 con detalle, como en /catalogs."""

    def boom(self, codigo_proveedor: str) -> list[dict]:  # noqa: ARG001
        raise RuntimeError("pg down")

    monkeypatch.setattr(catalogs_ep.PgVectorManager, "list_documents", boom)
    response = client.get(
        "/api/v1/catalogs/documents", params={"codigo_proveedor": "MSA"}
    )
    assert response.status_code == 500
    assert "documentos del proveedor" in response.json()["detail"]
