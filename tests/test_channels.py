"""Tests for the channel abstraction (PR1: channels)."""

from __future__ import annotations

import pytest

from src.channels.base import Channel, InboundMessage
from src.channels.telegram import TelegramChannel


class DummyChannel(Channel):
    name = "dummy"

    async def parse_inbound(self, payload: dict) -> InboundMessage:
        return InboundMessage(channel=self.name, sender_id=payload["sender"], text=payload["text"])

    def verify_request(self, payload: dict, signature: str | None) -> bool:
        return payload.get("secret") == "ok"

    async def send_text(self, sender_id: str, text: str) -> None:
        self.sent = (sender_id, text)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_channel_abc_contract_is_implemented():
    """A channel implements the full shared contract."""
    c = DummyChannel()
    assert isinstance(c, Channel)
    msg = await c.parse_inbound({"sender": "1", "text": "hola"})
    assert isinstance(msg, InboundMessage)
    assert msg.channel == "dummy"
    assert msg.sender_id == "1"
    assert msg.text == "hola"


def test_channel_verify_request():
    """The verify hook returns a boolean."""
    c = DummyChannel()
    assert c.verify_request({"secret": "ok"}, None) is True
    assert c.verify_request({"secret": "no"}, None) is False


@pytest.mark.asyncio
async def test_telegram_parse_inbound():
    """Telegram adapter normalizes a raw update into an InboundMessage."""
    c = TelegramChannel()
    payload = {"message": {"chat": {"id": 12345}, "text": "clavos 2 pulgadas"}}
    msg = await c.parse_inbound(payload)
    assert msg.channel == "telegram"
    assert msg.sender_id == "12345"
    assert msg.text == "clavos 2 pulgadas"


def test_telegram_verify_request_accepts_demo_payload():
    """Telegram demo channel accepts webhooks (authenticated by bot token)."""
    assert TelegramChannel().verify_request({}, None) is True
