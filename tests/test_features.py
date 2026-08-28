"""Per-Fase feature flag tests (task 5.2).

The FASE1..4_ENABLED settings gate each phase at its boundary: a disabled fase
refuses to run with ``FeatureDisabledError`` (webhook dispatch and backoffice
build) instead of half-working. All flags default to enabled.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api import webhook as webhook_module
from src.api.webhook import app
from src.backoffice.app import build_app
from src.config import Settings
from src.features import FeatureDisabledError, fase_enabled, require_fase


def test_all_fases_enabled_by_default():
    """Por defecto todas las fases están habilitadas."""
    for fase in (1, 2, 3, 4):
        assert fase_enabled(fase, Settings()) is True


def test_fase_enabled_reflects_flag():
    """El flag de una fase deshabilitada se refleja en fase_enabled."""
    assert fase_enabled(2, Settings(fase2_enabled=False)) is False
    assert fase_enabled(3, Settings(fase3_enabled=False)) is False


def test_require_fase_raises_when_disabled():
    """Deshabilitar una fase hace que require_fase lance FeatureDisabledError."""
    with pytest.raises(FeatureDisabledError, match="fase 2"):
        require_fase(2, Settings(fase2_enabled=False))


def test_require_fase_passes_when_enabled():
    """Con la fase habilitada, require_fase no lanza nada."""
    require_fase(4, Settings())


def test_unknown_fase_raises_value_error():
    """Una fase inexistente se rechaza con ValueError."""
    with pytest.raises(ValueError):
        fase_enabled(9)


def test_backoffice_build_refuses_when_fase4_disabled():
    """El backoffice no se construye cuando la fase 4 está deshabilitada."""
    with pytest.raises(FeatureDisabledError):
        build_app(settings=Settings(fase4_enabled=False))


def test_webhook_acks_without_dispatch_when_fase2_disabled():
    """Con la fase 2 apagada el webhook responde ACK sin despachar trabajo."""
    handled: list[object] = []

    def spy_handler(message):
        handled.append(message)

    disabled = Settings(fase2_enabled=False)
    with (
        patch.object(webhook_module, "ORCHESTRATOR_HANDLER", spy_handler),
        patch.object(webhook_module, "settings", disabled),
    ):
        client = TestClient(app)
        body = b'{"message": {"chat": {"id": 1}, "text": "hola"}}'
        signature = (
            "sha256=" + hmac.new(disabled.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        )
        response = client.post(
            "/webhook/telegram", content=body, headers={"x-hub-signature-256": signature}
        )
    assert response.status_code == 200
    assert response.text == "ACK"
    assert handled == []  # stop at the boundary: no heavy work dispatched
