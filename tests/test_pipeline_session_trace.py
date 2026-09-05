"""End-to-end integration test for session lifecycle and service tracing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.commands import GUIDED_ASK_CLIENT
from src.agents.customer import CustomerResponder
from src.agents.product_search import PrecedenceProductSearcher
from src.channels.base import InboundMessage
from src.config import Settings
from src.integrations.rag import RagProduct, RagProductClient
from src.observability.session_logger import list_session_files, read_session_events
from src.pipeline import build_orchestrator, handle_inbound


class FakeResponder(CustomerResponder):
    def respond(self, messages):
        return "Respuesta simulada"


class EmptyLocalSearcher:
    def search(self, query: str):
        return ()


@pytest.mark.asyncio
async def test_session_trace_lifecycle_and_rag_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El pipeline inicia sesión con 'Hola Bob', preserva la sesión y registra eventos de RAG."""
    # Point sessions dir to tmp_path for isolated test
    monkeypatch.setattr("src.observability.session_logger.DEFAULT_SESSIONS_DIR", tmp_path)

    fake_settings = Settings(
        owner_telegram_chat_id="tg_12345",
        telegram_bot_token="dummy_token",
    )
    monkeypatch.setattr("src.pipeline.get_settings", lambda: fake_settings)

    mock_channel = AsyncMock()
    mock_channel.send_text = AsyncMock()
    monkeypatch.setitem(
        __import__("src.channels", fromlist=["CHANNELS"]).CHANNELS,
        "telegram",
        mock_channel,
    )

    rag_client = RagProductClient()
    orchestrator = build_orchestrator(
        responder=FakeResponder(),
        searcher=PrecedenceProductSearcher(EmptyLocalSearcher(), rag_client),
    )
    monkeypatch.setattr("src.pipeline.ORCHESTRATOR", orchestrator)

    # 1. User sends "Hola Bob"
    msg1 = InboundMessage(channel="telegram", sender_id="tg_12345", text="Hola Bob")
    await handle_inbound(msg1)

    # Verify a session was created and logged
    sessions = list_session_files(log_dir=tmp_path)
    assert len(sessions) == 1
    sid1 = sessions[0]

    events1 = read_session_events(sid1, log_dir=tmp_path)
    actions1 = [e["action"] for e in events1]
    assert "inbound_message" in actions1
    assert "routing_decision" in actions1
    assert "outbound_reply" in actions1

    outbound1 = next(e for e in events1 if e["action"] == "outbound_reply")
    assert outbound1["details"]["reply"] == GUIDED_ASK_CLIENT

    # 2. Mock RAG client to return product when searched
    sample_product = RagProduct(sku="TORN-01", name="Tornillo autoperforante", provider="TEST")
    with patch.object(rag_client, "query", return_value=(sample_product,)):
        # User asks a product query in the same session
        msg2 = InboundMessage(
            channel="telegram", sender_id="tg_12345", text="tenés tornillo autoperforante?"
        )
        await handle_inbound(msg2)

    # Same session must have been updated
    sessions_after_msg2 = list_session_files(log_dir=tmp_path)
    assert len(sessions_after_msg2) == 1
    assert sessions_after_msg2[0] == sid1

    events2 = read_session_events(sid1, log_dir=tmp_path)
    assert len(events2) > len(events1)
    actions2 = [e["action"] for e in events2]
    assert actions2.count("inbound_message") == 2
    assert actions2.count("outbound_reply") == 2

    # 3. User sends "Hola Bob" again -> New session is spawned
    msg3 = InboundMessage(channel="telegram", sender_id="tg_12345", text="hola bob!")
    await handle_inbound(msg3)

    sessions_after_reset = list_session_files(log_dir=tmp_path)
    assert len(sessions_after_reset) == 2
    sid2 = next(s for s in sessions_after_reset if s != sid1)

    events3 = read_session_events(sid2, log_dir=tmp_path)
    assert len(events3) >= 3
    assert events3[0]["action"] == "inbound_message"
    assert events3[0]["details"]["text"] == "hola bob!"
