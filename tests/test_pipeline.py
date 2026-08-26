"""Walking-skeleton pipeline tests.

The pipeline composes the real orchestrator (routing + session store) with stub
agent handlers and bridges the reply back through the channel adapter. These
tests prove a message round-trips through routing and persistence and that the
reply reflects the routing decision — without OpenAI/Postgres/Sheets.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.channels.base import InboundMessage
from src.orchestrator.router import AgentName
from src.pipeline import build_orchestrator, handle_inbound


class FakeChannel:
    """In-memory channel adapter recording outbound sends."""

    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, sender_id: str, text: str) -> None:
        self.sent.append((sender_id, text))


def _message(
    *,
    text: str | None = None,
    media_type: str | None = None,
    sender: str = "+5491155551234",
) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id=sender, text=text, media_type=media_type)


def test_build_orchestrator_registers_all_six_agents():
    """El orquestador de la pipeline enlaza los seis agentes."""
    orchestrator = build_orchestrator()
    assert set(orchestrator.agents) == set(AgentName)


@pytest.mark.asyncio
async def test_handle_inbound_routes_persists_and_replies():
    """Un mensaje entrante se enruta, persiste contexto y responde por el canal."""
    channel = FakeChannel()
    orchestrator = build_orchestrator()
    with (
        patch("src.pipeline.CHANNELS", {"telegram": channel}),
        patch("src.pipeline.ORCHESTRATOR", orchestrator),
    ):
        await handle_inbound(_message(text="hola"))

    assert channel.sent == [("+5491155551234", "[orquestador] customer · pedido nuevo: hola")]
    assert orchestrator.store.get("+5491155551234") is not None


@pytest.mark.asyncio
async def test_second_message_resumes_context():
    """Un segundo mensaje del mismo remitente continúa el pedido (contexto persistido)."""
    channel = FakeChannel()
    orchestrator = build_orchestrator()
    with (
        patch("src.pipeline.CHANNELS", {"telegram": channel}),
        patch("src.pipeline.ORCHESTRATOR", orchestrator),
    ):
        await handle_inbound(_message(text="hola"))
        await handle_inbound(_message(text="quiero 10 clavos"))

    assert channel.sent[1][1] == (
        "[orquestador] customer · continuando el pedido: quiero 10 clavos"
    )


@pytest.mark.asyncio
async def test_voice_routes_to_perception_reply():
    """Una nota de voz se enruta a Percepción con una respuesta específica."""
    channel = FakeChannel()
    orchestrator = build_orchestrator()
    with (
        patch("src.pipeline.CHANNELS", {"telegram": channel}),
        patch("src.pipeline.ORCHESTRATOR", orchestrator),
    ):
        await handle_inbound(_message(media_type="voice"))

    assert channel.sent == [("+5491155551234", "Recibí tu nota de voz (transcripción pendiente).")]


@pytest.mark.asyncio
async def test_unknown_channel_drops_reply_without_crash():
    """Un canal sin adaptador no rompe la pipeline: descarta la respuesta."""
    orchestrator = build_orchestrator()
    with patch("src.pipeline.CHANNELS", {}), patch("src.pipeline.ORCHESTRATOR", orchestrator):
        await handle_inbound(InboundMessage(channel="unknown", sender_id="x", text="hola"))
