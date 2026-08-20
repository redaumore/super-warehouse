"""Telegram demo-channel adapter.

Implements the shared `Channel` contract on top of `python-telegram-bot`.
Used as the low-friction demo channel while WhatsApp is the production primary
(implemented in Phase 3).
"""

from __future__ import annotations

import logging

from src.channels.base import Channel, InboundMessage
from src.config import get_settings

logger = logging.getLogger(__name__)


class TelegramChannel(Channel):
    """Telegram adapter implementing the `Channel` ABC."""

    name = "telegram"

    def __init__(self) -> None:
        self.settings = get_settings()
        # Optional import kept local so importing this module does not require
        # a bot token or network at import time.
        from telegram import Bot

        self._bot = (
            Bot(token=self.settings.telegram_bot_token)
            if self.settings.telegram_bot_token
            else None
        )

    async def parse_inbound(self, payload: dict) -> InboundMessage:
        """Normalize a Telegram `update` payload into an `InboundMessage`."""
        message = payload.get("message") or {}
        chat = message.get("chat") or {}
        sender_id = str(chat.get("id", ""))
        text = message.get("text")
        return InboundMessage(channel=self.name, sender_id=sender_id, text=text, raw=payload)

    def verify_request(self, payload: dict, signature: str | None) -> bool:
        """Telegram webhooks are authenticated by the bot token itself.

        For the MVP demo we accept any payload (the endpoint is only exposed
        during the demo). Production would pin an allowed update source.
        """
        return True

    async def send_text(self, sender_id: str, text: str) -> None:
        """Send a text reply via the Telegram bot (no-op when no token set)."""
        if self._bot is None:
            logger.warning("Telegram bot token not configured; skipping send to %s", sender_id)
            return
        await self._bot.send_message(chat_id=sender_id, text=text)
