"""Pruebas del gate de autenticación del endpoint de adopción (tasks 2.6–2.7).

Cubren el contrato de ``src.api.adoption`` sin tocar la base de datos: HMAC
sobre el body crudo (falta/inválido → 401), allowlist de ``X-Owner-Id``
(fuera de lista → 403), allowlist vacía fail-closed (403) y el happy path
donde un owner permitido llega al handler con su ``OwnerContext``.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from src.api.adoption import app

SECRET = "test-secret"


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADOPTION_OWNER_SECRET", SECRET)
    monkeypatch.setenv("ADOPTION_OWNER_IDS", "owner-1,owner-2")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


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
    import json

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
    import json

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
    import json

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
    import json

    body = json.dumps(_VALID_BODY).encode()
    r = client.post("/adoption", content=body, headers={"x-signature": _sign(body)})
    assert r.status_code == 403


def test_allowlist_vacia_falla_cerrado_403(client, monkeypatch):
    """Allowlist vacía en env = fail-closed 403 aunque el HMAC sea válido."""
    import json

    monkeypatch.setenv("ADOPTION_OWNER_IDS", "")
    body = json.dumps(_VALID_BODY).encode()
    r = client.post(
        "/adoption",
        content=body,
        headers={"x-signature": _sign(body), "x-owner-id": "owner-1"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "owner_not_allowed"