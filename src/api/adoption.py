"""Endpoint de adopción de productos RAG, con gate HMAC + allowlist de owners.

App separada (``uvicorn src.api.adoption:app``), espejando el estilo de
``webhook.py``: un write síncrono no debe acoplarse al intake ACK<5s. El gate
lee el body crudo ANTES de parsear (declarar un parámetro tipado haría que
FastAPI consuma el body y ``request.body()`` devuelva vacío) y verifica un
HMAC-SHA256 hex con ``hmac.compare_digest`` (constante, sin filtrar longitud),
luego valida ``X-Owner-Id`` contra la allowlist — vacía = fail-closed 403.

El handler real (task 3.1) aplica ``require_fase(4, settings)`` como el
backoffice, corre ``adopt_product`` en una sesión propia y commitea; el
endpoint traduce cada error de dominio al error body del diseño:
401 invalid_hmac / 403 owner_not_allowed / 422 (invalid_body, invalid_stock,
supplier_unknown, supplier_inactive, missing_provenance) / 409 sku_collision /
502 (embedding_unavailable, rag_unavailable) / 503 feature_disabled.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.backoffice.adoption import (
    AdoptRequest,
    Embedder,
    EmbeddingUnavailableError,
    InvalidStockError,
    MissingProvenanceError,
    OwnerContext,
    SkuCollisionError,
    SupplierUnknownError,
    adopt_product,
)
from src.config import Settings, get_settings
from src.db.session import SessionLocal
from src.features import FeatureDisabledError, require_fase
from src.integrations.rag import RagProductError
from src.supplier.guards import SupplierInactiveError

app = FastAPI(title="super-warehouse adoption", version="0.1.0")

settings = get_settings()


def _default_embedder(cfg: Settings) -> Embedder:
    """Embedder real de OpenAI, con timeout/retries de la config de adopción.

    Se construye por request (el cliente SDK es lazy: sin ``OPENAI_API_KEY``
    la construcción no falla y el error llega como 502 al embedear).
    """
    from src.integrations.openai import OpenAIEmbedder

    return OpenAIEmbedder(
        settings=cfg,
        timeout=cfg.adoption_embed_timeout_seconds,
        retries=cfg.adoption_embed_retries,
    )


# Seam del embedder (los tests de integración lo reemplazan por un fake de
# vector fijo; producción usa OpenAI). Espeja el patrón de ADOPTION_HANDLER.
EMBEDDER_FACTORY: Callable[[Settings], Embedder] = _default_embedder


def _real_handler(dto: AdoptRequest, owner_ctx: OwnerContext) -> None:
    """Use case real post-gate: fase 4 gate, adopción y commit en una sesión.

    ``require_fase(4, settings)`` corta en el límite de Fase como el backoffice
    (``FeatureDisabledError`` → 503 en el endpoint). La sesión se abre por
    request; ante cualquier falla del use case se hace rollback y se re-lanza
    para que el endpoint traduzca el error al error body.
    """
    require_fase(4, settings)
    session = SessionLocal()
    try:
        adopt_product(session, dto, owner_ctx, EMBEDDER_FACTORY(settings))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Seam del handler post-gate: los tests lo reemplazan por un fake para probar
# el gate sin base de datos; en producción ejecuta el use case real.
ADOPTION_HANDLER: Callable[[AdoptRequest, OwnerContext], Awaitable[None] | None] | None = (
    _real_handler
)


def _owner_allowlist() -> list[str]:
    """Allowlist de owners desde Settings (CSV); vacía cierra."""
    return [item.strip() for item in settings.adoption_owner_ids.split(",") if item.strip()]


def _signature_is_valid(raw_body: bytes, signature: str | None) -> bool:
    """Verifica HMAC-SHA256 hex sobre el body crudo (comparación constante).

    Sin secreto configurado el endpoint cierra: un write de inventario no
    acepta requests sin credenciales. ``adoption_owner_secret_old`` se acepta
    durante la ventana de rotación (zero-downtime).
    """
    if not signature:
        return False
    secrets = [
        secret
        for secret in (settings.adoption_owner_secret, settings.adoption_owner_secret_old)
        if secret
    ]
    if not secrets:
        return False
    expected = (
        hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest() for secret in secrets
    )
    return any(hmac.compare_digest(candidate, signature) for candidate in expected)


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
    """Gate de autenticación + wiring: HMAC, allowlist, parseo y handler.

    Mapa de errores completo del diseño: 401 invalid_hmac / 403
    owner_not_allowed / 422 invalid_body · invalid_stock · supplier_unknown ·
    supplier_inactive · missing_provenance / 409 sku_collision / 502
    embedding_unavailable · rag_unavailable / 503 feature_disabled.
    """
    raw_body = await request.body()
    if not _signature_is_valid(raw_body, request.headers.get("x-signature")):
        return _error("invalid_hmac", "invalid signature", 401)
    owner_id = request.headers.get("x-owner-id")
    if not _owner_allowed(owner_id):
        return _error("owner_not_allowed", "owner not allowed", 403)
    assert owner_id is not None  # el gate solo deja pasar owners de la allowlist
    try:
        dto = AdoptRequest.model_validate_json(raw_body)
    except ValidationError as exc:
        return _error("invalid_body", str(exc), 422)
    owner_ctx = OwnerContext(owner_id=owner_id)
    if ADOPTION_HANDLER is None:
        return _error("service_unavailable", "adoption handler not configured", 503)
    try:
        result = ADOPTION_HANDLER(dto, owner_ctx)
        if result is not None:
            await result
    except FeatureDisabledError as exc:
        return _error("feature_disabled", str(exc), 503)
    except SupplierUnknownError as exc:
        return _error("supplier_unknown", str(exc), 422)
    except SupplierInactiveError as exc:
        return _error("supplier_inactive", str(exc), 422)
    except MissingProvenanceError as exc:
        return _error("missing_provenance", str(exc), 422)
    except InvalidStockError as exc:
        return _error("invalid_stock", str(exc), 422)
    except SkuCollisionError as exc:
        return _error("sku_collision", str(exc), 409)
    except EmbeddingUnavailableError as exc:
        return _error("embedding_unavailable", str(exc), 502)
    except RagProductError as exc:
        return _error("rag_unavailable", str(exc), 502)
    return Response(status_code=200, content="adopted")