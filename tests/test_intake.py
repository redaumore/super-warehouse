"""Intake integration tests (task 4.6): ACK <5 s and heavy work async.

The webhook must acknowledge well under the 5-second SLA and must hand heavy
processing (transcription, search, pricing) to a background task so the ACK
never waits on it. A recording ASGI transport captures the exact moment the
response body is sent; a slow orchestrator handler records when it actually
starts — the test asserts the ACK arrived first and the handler ran after.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import patch

import httpx
import pytest

from src.api import webhook as webhook_module
from src.api.webhook import app, settings

_TELEGRAM_BODY = b'{"message": {"chat": {"id": 1}, "text": "clavos 2 pulgadas"}}'


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()


class RecordingTransport(httpx.ASGITransport):
    """ASGI transport that records when the response body is sent."""

    def __init__(self, app) -> None:
        self.response_sent_at: float | None = None
        super().__init__(app=self._wrapped_app(app))

    def _wrapped_app(self, app):
        async def wrapped(scope, receive, send):
            async def recording_send(message):
                if message["type"] == "http.response.body" and self.response_sent_at is None:
                    self.response_sent_at = time.perf_counter()
                await send(message)

            await app(scope, receive, recording_send)

        return wrapped


@pytest.mark.asyncio
async def test_ack_returns_under_five_seconds_with_slow_handler():
    """El ACK responde en menos de 5 segundos aunque el trabajo pesado duerma."""
    def slow_handler(message):
        time.sleep(0.5)

    with patch.object(webhook_module, "ORCHESTRATOR_HANDLER", slow_handler):
        transport = RecordingTransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            start = time.perf_counter()
            response = await client.post(
                "/webhook/telegram",
                content=_TELEGRAM_BODY,
                headers={"x-hub-signature-256": _signature(_TELEGRAM_BODY)},
            )
            elapsed = time.perf_counter() - start
    assert response.status_code == 200
    assert response.text == "ACK"
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_heavy_work_runs_after_ack_is_sent():
    """El trabajo pesado se ejecuta en background, después del ACK."""
    started_at: dict[str, float] = {}

    def slow_handler(message):
        started_at["handler"] = time.perf_counter()

    with patch.object(webhook_module, "ORCHESTRATOR_HANDLER", slow_handler):
        transport = RecordingTransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/webhook/telegram",
                content=_TELEGRAM_BODY,
                headers={"x-hub-signature-256": _signature(_TELEGRAM_BODY)},
            )
    assert response.status_code == 200
    assert transport.response_sent_at is not None
    assert started_at["handler"] >= transport.response_sent_at


@pytest.mark.asyncio
async def test_handler_receives_normalized_inbound_message():
    """El handler de fondo recibe el mensaje entrante ya normalizado."""
    received: list[object] = []

    def spy_handler(message):
        received.append(message)

    with patch.object(webhook_module, "ORCHESTRATOR_HANDLER", spy_handler):
        transport = RecordingTransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/webhook/telegram",
                content=_TELEGRAM_BODY,
                headers={"x-hub-signature-256": _signature(_TELEGRAM_BODY)},
            )
    assert response.status_code == 200
    assert len(received) == 1
    assert received[0].text == "clavos 2 pulgadas"  # type: ignore[attr-defined]