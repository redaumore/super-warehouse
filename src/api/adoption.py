"""Endpoint de adopción de productos RAG, con gate HMAC + allowlist de owners.

App separada (``uvicorn src.api.adoption:app``), espejando el estilo de
``webhook.py``: un write síncrono no debe acoplarse al intake ACK<5s. El gate
lee el body crudo ANTES de parsear (declarar un parámetro tipado haría que
FastAPI consuma el body y ``request.body()`` devuelva vacío) y verifica un
HMAC-SHA256 hex con ``hmac.compare_digest`` (constante, sin filtrar longitud),
luego valida ``X-Owner-Id`` contra la allowlist — vacía = fail-closed 403.

Este módulo implementa el gate y los schemas; el wiring completo (use case,
mapa de errores 401/403/422/409/502, ``require_fase``) es la task 3.1.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from src.backoffice.adoption import AdoptRequest, OwnerContext

app = FastAPI(title="super-warehouse adoption", version="0.1.0")

# Seam del handler post-gate (el use case + commit llegan en la task 3.1). Los
# tests lo reemplazan por un fake para probar el gate sin base de datos.
ADOPTION_HANDLER: Callable[[AdoptRequest, OwnerContext], Awaitable[None] | None] | None = None


def _owner_allowlist() -> list[str]:
    """Allowlist de owners desde ``ADOPTION_OWNER_IDS`` (CSV); vacía cierra."""
    raw = os.environ.get("ADOPTION_OWNER_IDS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _signature_is_valid(raw_body: bytes, signature: str | None) -> bool:
    """Verifica HMAC-SHA256 hex sobre el body crudo (comparación constante).

    Sin secreto configurado el endpoint cierra: un write de inventario no
    acepta requests sin credenciales.
    """
    secret = os.environ.get("ADOPTION_OWNER_SECRET", "")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _owner_allowed(owner_id: str | None) -> bool:
    """Membership en la allowlist, comparado en tiempo constante; vacía = 403."""
    allowlist = _owner_allowlist()
    if not allowlist or not owner_id:
        return False
    return any(hmac.compare_digest(owner_id, allowed) for allowed in allowlist)


def _error(code: str, message: str, status: int) -> JSONResponse:
    """Error body del diseño: ``{"error": {"code", "message"}}``."""
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.post("/adoption")
async def adopt(request: Request) -> Response:
    """Gate de autenticación: HMAC del body crudo, luego allowlist del owner."""
    raw_body = await request.body()
    if not _signature_is_valid(raw_body, request.headers.get("x-signature")):
        return _error("invalid_hmac", "invalid signature", 401)
    owner_id = request.headers.get("x-owner-id")
    if not _owner_allowed(owner_id):
        return _error("owner_not_allowed", "owner not allowed", 403)
    assert owner_id is not None  # el gate solo deja pasar owners de la allowlist
    dto = AdoptRequest.model_validate_json(raw_body)
    owner_ctx = OwnerContext(owner_id=owner_id)
    if ADOPTION_HANDLER is not None:
        result = ADOPTION_HANDLER(dto, owner_ctx)
        if result is not None:
            await result
    return Response(status_code=200, content="adopted")