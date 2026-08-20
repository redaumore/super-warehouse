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


@pytest.fixture
def client():
    return TestClient(app)


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()


def test_healthz(client):
    assert client.get("/healthz").status_code == 200


def test_unknown_channel_returns_404(client):
    r = client.post("/webhook/unknown", json={})
    assert r.status_code == 404


def test_ack_returns_quickly(client):
    """ACK is returned well under the 5-second SLA (happy path)."""
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
    """A payload without a valid signature is rejected with 401."""
    body = b'{"message": {"chat": {"id": 1}}}'
    r = client.post("/webhook/telegram", content=body)
    assert r.status_code == 401


def test_bad_signature_rejected(client):
    """A payload with a wrong signature is rejected with 401."""
    body = b'{"message": {"chat": {"id": 1}}}'
    bad = "sha256=" + "0" * 64
    r = client.post("/webhook/telegram", content=body, headers={"x-hub-signature-256": bad})
    assert r.status_code == 401
