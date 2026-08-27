"""Tests for the intake webhook (PR1): signature verification + ACK <5s.

The webhook MUST ACK well under the 5-second SLA and MUST reject
unauthenticated payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from src.api.webhook import app, settings
from src.channels.telegram import TelegramChannel


class _FakeResponder:
    """Deterministic fake LLM responder so the pipeline never calls OpenAI."""

    def respond(self, messages) -> str:
        return "respuesta del modelo"


@pytest.fixture(autouse=True)
def _no_telegram_network(monkeypatch):
    """Keep webhook ACK tests offline: the walking-skeleton reply must not post.

    The intake now wires the pipeline handler by default, so a 200 response on
    the Telegram endpoint dispatches a background reply. No-op the send so the
    ACK latency assertion stays deterministic and no network call leaks out.
    The pipeline orchestrator is also swapped for a fake-responder build so the
    Customer agent never reaches OpenAI, and a fake catalog searcher so it
    never reaches Postgres, regardless of env credentials.
    """
    from src.pipeline import build_orchestrator
    from tests.test_customer import FakeSearcher

    async def _noop(self, sender_id, text):
        return None

    monkeypatch.setattr(TelegramChannel, "send_text", _noop)
    monkeypatch.setattr(
        "src.pipeline.ORCHESTRATOR",
        build_orchestrator(responder=_FakeResponder(), searcher=FakeSearcher()),
    )


@pytest.fixture
def client():
    return TestClient(app)


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()


def test_healthz(client):
    """El endpoint de salud responde 200."""
    assert client.get("/healthz").status_code == 200


def test_unknown_channel_returns_404(client):
    """Un canal desconocido devuelve 404."""
    r = client.post("/webhook/unknown", json={})
    assert r.status_code == 404


def test_ack_returns_quickly(client):
    """El webhook confirma (ACK) muy por debajo del SLA de 5 segundos.

    ACK is returned well under the 5-second SLA (happy path).
    """
    body = b'{"message": {"chat": {"id": 1}, "text": "hola"}}'
    start = time.perf_counter()
    r = client.post(
        "/webhook/telegram", content=body, headers={"x-hub-signature-256": _signature(body)}
    )
    elapsed = time.perf_counter() - start
    assert r.status_code == 200
    assert r.text == "ACK"
    assert elapsed < 5.0


def test_unauthenticated_payload_rejected(client):
    """Un payload de WhatsApp sin firma válida se rechaza con 401.

    A WhatsApp payload without a valid HMAC signature is rejected with 401.
    """
    body = b'{"message": {"chat": {"id": 1}}}'
    r = client.post("/webhook/whatsapp", content=body)
    assert r.status_code == 401


def test_bad_signature_rejected(client):
    """Un payload de WhatsApp con firma incorrecta se rechaza con 401.

    A WhatsApp payload with a wrong HMAC signature is rejected with 401.
    """
    body = b'{"message": {"chat": {"id": 1}}}'
    bad = "sha256=" + "0" * 64
    r = client.post("/webhook/whatsapp", content=body, headers={"x-hub-signature-256": bad})
    assert r.status_code == 401


def test_telegram_webhook_requires_secret_token_when_configured(client, monkeypatch):
    """Con token secreto configurado, Telegram exige el header de autenticación.

    With a secret token configured, the Telegram webhook rejects requests that
    omit it or send the wrong value, and accepts a matching header.
    """
    monkeypatch.setattr(settings, "telegram_secret_token", "top-secret")
    body = b'{"message": {"chat": {"id": 1}, "text": "hola"}}'
    # Missing header → rejected.
    assert client.post("/webhook/telegram", content=body).status_code == 401
    # Wrong header → rejected.
    wrong = client.post(
        "/webhook/telegram",
        content=body,
        headers={"x-telegram-bot-api-secret-token": "wrong"},
    )
    assert wrong.status_code == 401
    # Matching header → accepted.
    ok = client.post(
        "/webhook/telegram",
        content=body,
        headers={"x-telegram-bot-api-secret-token": "top-secret"},
    )
    assert ok.status_code == 200
    assert ok.text == "ACK"
