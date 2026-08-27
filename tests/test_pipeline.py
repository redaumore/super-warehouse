"""Walking-skeleton pipeline tests.

The pipeline composes the real orchestrator (routing + session store) with stub
agent handlers and bridges the reply back through the channel adapter. The
Customer agent is wired to a deterministic fake responder and a fake catalog
searcher so these tests prove a message round-trips through routing and
persistence — without OpenAI/Postgres/Sheets.
"""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import patch

import pytest

from src.agents.customer import CustomerResponder
from src.channels.base import InboundMessage
from src.orchestrator.router import AgentName
from src.orchestrator.session import ChatMessage
from src.pipeline import build_orchestrator, handle_inbound
from tests.test_customer import FakeSearcher


class FakeChannel:
    """In-memory channel adapter recording outbound sends."""

    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, sender_id: str, text: str) -> None:
        self.sent.append((sender_id, text))


class FakeResponder(CustomerResponder):
    """Deterministic fake LLM responder; records message lists, no network."""

    def __init__(self) -> None:
        self.reply = "respuesta del modelo"
        self.calls: list[Sequence[ChatMessage]] = []

    def respond(self, messages: Sequence[ChatMessage]) -> str:
        self.calls.append(messages)
        return self.reply


def _message(
    *,
    text: str | None = None,
    media_type: str | None = None,
    sender: str = "+5491155551234",
) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id=sender, text=text, media_type=media_type)


def test_build_orchestrator_registers_all_six_agents():
    """El orquestador de la pipeline enlaza los seis agentes."""
    orchestrator = build_orchestrator(responder=FakeResponder(), searcher=FakeSearcher())
    assert set(orchestrator.agents) == set(AgentName)


@pytest.mark.asyncio
async def test_handle_inbound_routes_persists_and_replies():
    """Un mensaje de Telegram nuevo es respondido por el agente Customer a través del responder LLM."""
    channel = FakeChannel()
    responder = FakeResponder()
    orchestrator = build_orchestrator(responder=responder, searcher=FakeSearcher())
    with (
        patch("src.pipeline.CHANNELS", {"telegram": channel}),
        patch("src.pipeline.ORCHESTRATOR", orchestrator),
    ):
        await handle_inbound(_message(text="hola"))

    assert channel.sent == [("+5491155551234", "respuesta del modelo")]
    state = orchestrator.store.get("+5491155551234")
    assert state is not None
    assert state.history == (
        ChatMessage("user", "hola"),
        ChatMessage("assistant", "respuesta del modelo"),
    )


@pytest.mark.asyncio
async def test_second_message_resumes_context():
    """Un segundo mensaje del mismo remitente continúa la conversación con el responder LLM."""
    channel = FakeChannel()
    responder = FakeResponder()
    orchestrator = build_orchestrator(responder=responder, searcher=FakeSearcher())
    with (
        patch("src.pipeline.CHANNELS", {"telegram": channel}),
        patch("src.pipeline.ORCHESTRATOR", orchestrator),
    ):
        await handle_inbound(_message(text="hola"))
        await handle_inbound(_message(text="quiero 10 clavos"))

    assert channel.sent[1][1] == "respuesta del modelo"
    state = orchestrator.store.get("+5491155551234")
    assert state is not None
    assert [m.role for m in state.history] == ["user", "assistant", "user", "assistant"]
    assert len(responder.calls) == 2


@pytest.mark.asyncio
async def test_voice_routes_to_perception_reply():
    """Una nota de voz se enruta a Percepción con una respuesta específica."""
    channel = FakeChannel()
    orchestrator = build_orchestrator(responder=FakeResponder(), searcher=FakeSearcher())
    with (
        patch("src.pipeline.CHANNELS", {"telegram": channel}),
        patch("src.pipeline.ORCHESTRATOR", orchestrator),
    ):
        await handle_inbound(_message(media_type="voice"))

    assert channel.sent == [("+5491155551234", "Recibí tu nota de voz (transcripción pendiente).")]


@pytest.mark.asyncio
async def test_unknown_channel_drops_reply_without_crash():
    """Un canal sin adaptador no rompe la pipeline: descarta la respuesta."""
    orchestrator = build_orchestrator(responder=FakeResponder(), searcher=FakeSearcher())
    with patch("src.pipeline.CHANNELS", {}), patch("src.pipeline.ORCHESTRATOR", orchestrator):
        await handle_inbound(InboundMessage(channel="unknown", sender_id="x", text="hola"))
