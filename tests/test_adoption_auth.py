"""Pruebas del gate y del wiring del endpoint de adopción (tasks 2.6–3.2).

Cubren el contrato de ``src.api.adoption`` sin tocar la base de datos: HMAC
sobre el body crudo (falta/inválido → 401, rotación con secreto viejo), 
allowlist de ``X-Owner-Id`` (fuera de lista → 403, vacía fail-closed), parseo
de ``AdoptRequest`` (body inválido → 422), feature flag fase 4 (→ 503) y la
fábrica de embedder que consume los settings de adopción.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.api.adoption import _default_embedder, app, settings

SECRET = "test-secret"
OLD_SECRET = "old-test-secret"


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "adoption_owner_secret", SECRET)
    monkeypatch.setattr(settings, "adoption_owner_secret_old", "")
    monkeypatch.setattr(settings, "adoption_owner_ids", "owner-1,owner-2")
    monkeypatch.setattr(settings, "fase4_enabled", True)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


_VALID_BODY = {
    "sku": "at-5044",
    "nombre": "Tornillo 5/16 x 2 pulgadas",
    "codigo_proveedor": "AMX",
    "marca": "Acme",
    "categoria": "Fijaciones",
    "subcategoria": "Tornillos",
    "precio": 125.0,
    "moneda": "usd",
    "archivo_origen": "catalogo_amx_2026.pdf",
    "pagina": 12,
    "node_id": "node-abc-123",
    "stock": 50,
}


def test_hmac_valido_entrega_handler_con_ownercontext(client, monkeypatch):
    """Un HMAC válido con owner permitido llega al handler con su OwnerContext."""
    from src.api.adoption import OwnerContext

    calls: list[tuple[object, OwnerContext]] = []

    async def fake_handler(dto, owner_ctx):
        calls.append((dto, owner_ctx))

    monkeypatch.setattr("src.api.adoption.ADOPTION_HANDLER", fake_handler)

    body = json.dumps(_VALID_BODY).encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": _sign(body), "x-owner-id": "owner-1"},
    )
    assert r.status_code == 200
    assert r.text == "adopted"
    assert len(calls) == 1
    dto, owner_ctx = calls[0]
    assert dto.node_id == "node-abc-123"  # type: ignore[attr-defined]
    assert owner_ctx.owner_id == "owner-1"


def test_firma_ausente_rechazada_401(client):
    """Un request sin X-Signature se rechaza con 401 y no llega al handler."""
    r = client.post("/adoption", json=_VALID_BODY, headers={"x-owner-id": "owner-1"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_hmac"


def test_firma_invalida_rechazada_401(client):
    """Un HMAC incorrecto se rechaza con 401 (comparación constante)."""
    body = json.dumps(_VALID_BODY).encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": "0" * 64, "x-owner-id": "owner-1"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_hmac"


def test_owner_fuera_de_allowlist_rechazado_403(client):
    """Un owner no permitido con HMAC válido se rechaza con 403 y no persiste nada."""
    body = json.dumps(_VALID_BODY).encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": _sign(body), "x-owner-id": "owner-3"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "owner_not_allowed"


def test_owner_header_ausente_rechazado_403(client):
    """Sin X-Owner-Id el gate cierra con 403 (no hay identidad que auditar)."""
    body = json.dumps(_VALID_BODY).encode()
    r = client.post("/adoption", content=body, headers={"x-signature": _sign(body)})
    assert r.status_code == 403


def test_allowlist_vacia_falla_cerrado_403(client, monkeypatch):
    """Allowlist vacía en settings = fail-closed 403 aunque el HMAC sea válido."""
    monkeypatch.setattr(settings, "adoption_owner_ids", "")
    body = json.dumps(_VALID_BODY).encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": _sign(body), "x-owner-id": "owner-1"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "owner_not_allowed"


def test_secreto_viejo_aceptado_durante_rotacion(client, monkeypatch):
    """Durante la rotación el secreto viejo sigue firmando válido (zero-downtime)."""
    monkeypatch.setattr(settings, "adoption_owner_secret_old", OLD_SECRET)
    calls: list[object] = []

    async def fake_handler(dto, owner_ctx):
        calls.append(dto)

    monkeypatch.setattr("src.api.adoption.ADOPTION_HANDLER", fake_handler)

    body = json.dumps(_VALID_BODY).encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": _sign(body, OLD_SECRET), "x-owner-id": "owner-1"},
    )
    assert r.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        json.dumps({**_VALID_BODY, "stock": "mucho"}),
        json.dumps({**_VALID_BODY, "node_id": None}),
    ],
    ids=["json-invalido", "stock-no-entero", "node-id-null"],
)
def test_body_invalido_rechazado_422(client, raw):
    """Un body que no parsea a AdoptRequest con HMAC válido se rechaza con 422."""
    body = raw.encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": _sign(body), "x-owner-id": "owner-1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_body"


def test_fase4_deshabilitada_rechaza_503(client, monkeypatch):
    """Con fase 4 deshabilitada el handler real corta en el límite (503)."""
    monkeypatch.setattr(settings, "fase4_enabled", False)
    body = json.dumps(_VALID_BODY).encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": _sign(body), "x-owner-id": "owner-1"},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "feature_disabled"


def test_embedder_factory_aplica_timeout_y_retries_de_settings():
    """La fábrica de embedder consume adoption_embed_timeout/retries de Settings."""
    embedder = _default_embedder(settings)
    assert embedder.timeout == settings.adoption_embed_timeout_seconds
    assert embedder.retries == settings.adoption_embed_retries