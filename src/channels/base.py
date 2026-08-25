"""Channel abstraction.

A shared interface for inbound customer channels (WhatsApp primary, Telegram as
the in-MVP demo channel). Each adapter implements the same common contract so
the orchestrator is channel-agnostic: it receives a normalized inbound message
and can send a reply.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class InboundMessage:
    """Normalized inbound message independent of the underlying channel."""

    channel: str
    sender_id: str  # normalized phone / chat id for the sender
    text: str | None = None
    media_url: str | None = None
    media_type: str | None = None  # e.g. "voice", "image", "document"
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    raw: dict[str, Any] = field(default_factory=dict)


class Channel(ABC):
    """Common contract every channel adapter must satisfy."""

    name: str

    @abstractmethod
    async def parse_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        """Normalize a raw channel webhook payload into an `InboundMessage`."""

    @abstractmethod
    def verify_request(
        self, payload: dict[str, Any], signature: str | None, secret_token: str | None = None
    ) -> bool:
        """Return True when the inbound request is authentic (signature / token)."""

    @abstractmethod
    async def send_text(self, sender_id: str, text: str) -> None:
        """Send a text reply to a sender on this channel."""
