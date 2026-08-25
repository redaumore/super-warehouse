"""Intake webhook.

Accepts inbound order messages from active channels, verifies authenticity,
ACKs immediately (well under the 5 s SLA) and hands heavy processing to the
background so the webhook is never blocked by transcription/search/pricing.

Phase 1 delivers the skeleton + signature verification + ephemeral ACK. The
orchestrator dispatch hook is the seam where Phase 3 wires real background work.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable

from fastapi import BackgroundTasks, FastAPI, Request, Response

from src.channels.base import Channel, InboundMessage
from src.channels.telegram import TelegramChannel
from src.channels.whatsapp import WhatsAppChannel
from src.config import get_settings
from src.features import FeatureDisabledError, require_fase

logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(title="super-warehouse intake", version="0.1.0")

# Registered channel adapters keyed by channel name.
CHANNELS: dict[str, Channel] = {
    "telegram": TelegramChannel(),
    "whatsapp": WhatsAppChannel(),
}

# Seam for the orchestrator to consume a normalized inbound message (Phase 3).
ORCHESTRATOR_HANDLER: Callable[[InboundMessage], None] | None = None


def _signature_is_valid(payload_body: bytes, signature: str | None) -> bool:
    """Verify an HMAC-SHA256 signature over the raw request body.

    Used by channels that sign their payloads (e.g. WhatsApp Cloud API uses an
    `X-Hub-Signature-256` header). Telegram is authenticated by the bot token
    and returns True from its own `verify_request`.
    """
    if not signature:
        return False
    expected = (
        "sha256="
        + hmac.new(settings.webhook_secret.encode(), payload_body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.post("/webhook/{channel}")
async def webhook(
    channel: str, request: Request, background_tasks: BackgroundTasks
) -> Response:
    """Intake endpoint: verify, ACK <5 s, dispatch heavy work in the background."""
    adapter = CHANNELS.get(channel)
    if adapter is None:
        return Response(status_code=404, content="unknown channel")

    raw_body: bytes = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    secret_token = request.headers.get("x-telegram-bot-api-secret-token")

    # Authenticity gate. WhatsApp signs its payloads with an HMAC over the raw
    # body (X-Hub-Signature-256); Telegram authenticates webhooks with a secret
    # token (set at setWebhook time and echoed in a header) instead. A channel
    # may additionally enforce its own check, but a request that fails the
    # channel-specific gate is always rejected.
    if channel != "telegram" and not _signature_is_valid(raw_body, signature):
        logger.warning("Rejected unauthenticated webhook on channel=%s", channel)
        return Response(status_code=401, content="invalid signature")

    payload = await request.json()
    if not adapter.verify_request(payload, signature, secret_token):
        logger.warning("Rejected webhook failed channel verification on channel=%s", channel)
        return Response(status_code=401, content="invalid signature")

    message = await adapter.parse_inbound(payload)

    # ACK immediately — the client must not wait on heavy work. The orchestrator
    # handler runs as a background task AFTER the response is sent, so the ACK
    # stays well under the 5 s SLA regardless of transcription/search/pricing.
    # Fase 2 (the agent pipeline) is a safe stop point: when disabled, the
    # intake still ACKs but does not dispatch heavy work.
    if ORCHESTRATOR_HANDLER is not None:
        try:
            require_fase(2, settings)
        except FeatureDisabledError:
            logger.warning("fase 2 disabled: intake ACKs without dispatching work")
        else:
            background_tasks.add_task(ORCHESTRATOR_HANDLER, message)

    return Response(status_code=200, content="ACK")
