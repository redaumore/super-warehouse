"""WhatsApp Cloud API channel adapter (task 3.1).

Implements the shared ``Channel`` contract on top of the WhatsApp Cloud API
(the production primary channel; Telegram remains the demo channel):

- ``parse_inbound`` — normalizes a Cloud API webhook payload (text, voice,
  audio, image or document) into an ``InboundMessage``; media references are
  kept in ``raw`` (``media_id`` / ``mime_type``) because resolving the actual
  bytes requires an authenticated Graph API round-trip (``fetch_media``);
- ``verify_request`` — accepts the webhook subscription verification token
  (hub.*) and defers to the endpoint-level HMAC check (``X-Hub-Signature-256``
  over the raw body, computed by ``src.api.webhook``) for message payloads;
- ``send_text`` — sends a text reply to a WhatsApp number via the Graph API
  ``messages`` endpoint;
- ``fetch_media`` — resolves a media id to its bytes (id → media URL → GET with
  the bearer token), used by the orchestrator when heavy work needs the file.

Every network call goes through ``httpx`` and is mocked in the unit tests; no
token is required at import time.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.channels.base import Channel, InboundMessage
from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com"
_API_VERSION = "v19.0"

# WhatsApp message-part keys mapped to our normalized media kinds.
_MEDIA_KINDS: tuple[tuple[str, str], ...] = (
    ("voice", "voice"),
    ("audio", "voice"),
    ("image", "image"),
    ("document", "document"),
)


class WhatsAppError(Exception):
    """Base error for WhatsApp Cloud API failures (send or media fetch)."""


def _first_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the first message object from a Cloud API webhook payload."""
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            messages = value.get("messages") or []
            if messages:
                message: dict[str, Any] = messages[0]
                return message
    return None


class WhatsAppChannel(Channel):
    """WhatsApp Cloud API adapter implementing the ``Channel`` ABC."""

    name = "whatsapp"

    def __init__(self, settings: Settings | None = None) -> None:
        # Injectable for tests (get_settings is lru_cached; env-driven values
        # must not leak between tests).
        self.settings = settings or get_settings()

    def _extract_media(self, message: dict[str, Any]) -> tuple[str | None, str, str]:
        """Return ``(media_kind, media_id, mime_type)`` for a message part."""
        for key, kind in _MEDIA_KINDS:
            media = message.get(key)
            if media:
                media_id = str(media.get("id", ""))
                mime_type = str(media.get("mime_type", "") or "")
                return kind, media_id, mime_type
        return None, "", ""

    async def parse_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        """Normalize a Cloud API webhook payload into an ``InboundMessage``."""
        message = _first_message(payload)
        if message is None:
            return InboundMessage(channel=self.name, sender_id="", raw=payload)
        sender_id = str(message.get("from", ""))
        media_kind, media_id, mime_type = self._extract_media(message)
        text: str | None = None
        if media_kind is None:
            text = (message.get("text") or {}).get("body")
        raw: dict[str, Any] = dict(message)
        raw["media_id"] = media_id
        raw["mime_type"] = mime_type
        return InboundMessage(
            channel=self.name,
            sender_id=sender_id,
            text=text,
            media_type=media_kind,
            raw=raw,
        )

    def verify_request(self, payload: dict[str, Any], signature: str | None) -> bool:
        """Verify a WhatsApp webhook request.

        Subscription-verification payloads (``hub.verify_token``) are checked
        against the configured verify token. Message payloads arrive with the
        ``X-Hub-Signature-256`` header whose HMAC the intake endpoint already
        validated over the raw body; a present signature is authoritative here.
        """
        if "hub.verify_token" in payload:
            return payload.get("hub.verify_token") == self.settings.whatsapp_verify_token
        return signature is not None

    def _auth_headers(self) -> dict[str, str]:
        token = self.settings.whatsapp_token
        if not token:
            raise WhatsAppError("whatsapp token not configured")
        return {"Authorization": f"Bearer {token}"}

    async def send_text(self, sender_id: str, text: str) -> None:
        """Send a text reply via the Graph API (no-op when not configured)."""
        if not self.settings.whatsapp_token or not self.settings.whatsapp_phone_id:
            logger.warning("WhatsApp not configured; skipping send to %s", sender_id)
            return
        payload = {
            "messaging_product": "whatsapp",
            "to": sender_id,
            "type": "text",
            "text": {"body": text},
        }
        url = f"{_GRAPH_BASE}/{_API_VERSION}/{self.settings.whatsapp_phone_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, headers=self._auth_headers(), json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"whatsapp send failed: {exc}") from exc

    async def fetch_media(self, media_id: str) -> bytes:
        """Resolve a media id to its raw bytes via the Graph API."""
        headers = self._auth_headers()  # fail fast before any network call
        url = f"{_GRAPH_BASE}/{_API_VERSION}/{media_id}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                meta = await client.get(url, headers=headers)
                meta.raise_for_status()
                media_url = str(meta.json()["url"])
                data = await client.get(media_url, headers=headers)
                data.raise_for_status()
                return data.content
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"whatsapp media fetch failed: {exc}") from exc