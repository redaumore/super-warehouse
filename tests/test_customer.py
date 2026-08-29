"""Customer conversational handler tests (slice 2 of the final wiring).

The Customer agent now answers through a mockable LLM responder and injects a
transient catalog context note (via a fake searcher) before the user turn. The
store's greeting survives only as the fallback when the responder is
unconfigured or the message carries no text. Every test uses fakes — no
network, no database.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import OpenAI
from sqlalchemy.exc import SQLAlchemyError

from src.agents.customer import (
    GREETING,
    SYSTEM_PROMPT,
    CustomerResponder,
    ResponderError,
    ResponderNotConfigured,
    build_handler,
)
from src.agents.disambiguation import SearchCandidate
from src.channels.base import InboundMessage
from src.config import Settings
from src.integrations.openai import OpenAIResponder
from src.orchestrator.router import AgentName, RoutingDecision
from src.orchestrator.session import ChatMessage, ConversationState

SENDER = "+5491155551234"


class FakeResponder(CustomerResponder):
    """Deterministic fake responder recording the message lists it receives."""

    def __init__(self, reply: str = "respuesta del modelo", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[Sequence[ChatMessage]] = []

    def respond(self, messages: Sequence[ChatMessage]) -> str:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.reply


class FakeSearcher:
    """Deterministic fake searcher: records queries, returns configured candidates, can raise."""

    def __init__(
        self,
        candidates: tuple[SearchCandidate, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.candidates = candidates
        self.error = error
        self.queries: list[str] = []

    def search(self, query: str) -> tuple[SearchCandidate, ...]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.candidates


def _message(text: str | None = None, sender: str = SENDER) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id=sender, text=text)


def _decision() -> RoutingDecision:
    return RoutingDecision(agent=AgentName.CUSTOMER)


def _roles(messages: Sequence[ChatMessage]) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in messages]


def test_fresh_text_message_goes_to_responder_with_system_and_user():
    """Un mensaje nuevo le llega al responder con el system prompt y el turno del usuario."""
    fake = FakeResponder()
    handler = build_handler(fake)
    outcome = handler(_message(text="quiero 10 clavos"), None, _decision())

    assert _roles(fake.calls[0]) == [("system", SYSTEM_PROMPT), ("user", "quiero 10 clavos")]
    assert outcome.reply == "respuesta del modelo"
    assert outcome.state is not None
    assert outcome.state.sender_id == SENDER
    assert outcome.state.history == (
        ChatMessage("user", "quiero 10 clavos"),
        ChatMessage("assistant", "respuesta del modelo"),
    )


def test_continuing_conversation_sends_history_and_appends_new_pair():
    """Una conversación en curso le pasa el historial completo al responder y agrega el nuevo par."""
    fake = FakeResponder()
    handler = build_handler(fake)
    prev_history = (
        ChatMessage("user", "hola"),
        ChatMessage("assistant", "¿qué andás buscando?"),
    )
    state = ConversationState(sender_id=SENDER, history=prev_history)
    outcome = handler(_message(text="10 clavos"), state, _decision())

    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        ("user", "hola"),
        ("assistant", "¿qué andás buscando?"),
        ("user", "10 clavos"),
    ]
    assert outcome.state is not None
    assert outcome.state.history == prev_history + (
        ChatMessage("user", "10 clavos"),
        ChatMessage("assistant", "respuesta del modelo"),
    )


def test_unconfigured_responder_falls_back_to_greeting_and_logs_turns():
    """Sin clave de API el responder falla al saludo y el historial igual registra el turno."""
    fake = FakeResponder(error=ResponderNotConfigured("no key"))
    handler = build_handler(fake)
    outcome = handler(_message(text="hola"), None, _decision())

    assert outcome.reply == GREETING
    assert outcome.state is not None
    assert outcome.state.history == (
        ChatMessage("user", "hola"),
        ChatMessage("assistant", GREETING),
    )


def test_textless_message_greets_without_calling_responder():
    """Un mensaje sin texto saluda sin consultar al responder ni registrar turno de usuario."""
    fake = FakeResponder()
    handler = build_handler(fake)
    outcome = handler(_message(text=None), None, _decision())

    assert fake.calls == []
    assert outcome.reply == GREETING
    assert outcome.state is not None
    assert outcome.state.history == (ChatMessage("assistant", GREETING),)


def test_build_handler_uses_custom_fallback_and_system_prompt():
    """El handler respeta el fallback y el system prompt personalizados que recibe."""
    fake = FakeResponder()
    handler = build_handler(fake, fallback_reply="sin respuesta", system_prompt="Sos un probador.")

    textless = handler(_message(text=""), None, _decision())
    assert textless.reply == "sin respuesta"

    handler(_message(text="clavos"), None, _decision())
    assert fake.calls[0][0] == ChatMessage("system", "Sos un probador.")


def test_openai_responder_raises_not_configured_without_key():
    """Sin OPENAI_API_KEY, el responder OpenAI lanza ResponderNotConfigured."""
    responder = OpenAIResponder(settings=Settings(openai_api_key=""))
    with pytest.raises(ResponderNotConfigured):
        responder.respond([ChatMessage("user", "hola")])


def test_openai_responder_maps_messages_and_returns_model_text():
    """El responder OpenAI mapea roles y contenido y devuelve el texto del modelo."""
    client = MagicMock(spec=OpenAI)
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  dale, contame qué buscás  "))]
    )
    responder = OpenAIResponder(client=client, settings=Settings(openai_api_key="sk-test"))
    reply = responder.respond([ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", "clavos")])

    assert reply == "dale, contame qué buscás"
    call = client.chat.completions.create.call_args
    assert call.kwargs["model"] == "gpt-4o-mini"
    assert call.kwargs["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "clavos"},
    ]


def test_openai_responder_raises_when_model_returns_empty_reply():
    """Un modelo que no produce texto dispara ResponderError."""
    client = MagicMock(spec=OpenAI)
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
    )
    responder = OpenAIResponder(client=client, settings=Settings(openai_api_key="sk-test"))
    with pytest.raises(ResponderError, match="no reply"):
        responder.respond([ChatMessage("user", "clavos")])


NO_STOCK_NOTE = (
    "Búsqueda en catálogo para «¿tienen tarugos fisher?»: sin resultados. "
    "Si el cliente pidió un producto que no está en stock, decíselo al dueño."
)


def test_product_query_with_empty_catalog_injects_no_stock_note():
    """Con catálogo vacío el responder recibe la nota 'sin resultados' y el historial no la guarda."""
    fake = FakeResponder()
    searcher = FakeSearcher(candidates=())
    handler = build_handler(fake, searcher=searcher)

    outcome = handler(_message(text="¿tienen tarugos fisher?"), None, _decision())

    assert searcher.queries == ["¿tienen tarugos fisher?"]
    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        ("system", NO_STOCK_NOTE),
        ("user", "¿tienen tarugos fisher?"),
    ]
    assert outcome.state is not None
    assert [m.role for m in outcome.state.history] == ["user", "assistant"]


def test_catalog_candidates_become_note_listing_names_and_skus():
    """Con candidatos, la nota lista nombre oficial y SKU de cada producto."""
    candidates = (
        SearchCandidate(sku="SKU-001", nombre_oficial="Tarugo Fischer 8mm", confidence=0.97),
        SearchCandidate(sku="SKU-002", nombre_oficial="Tarugo Fischer 10mm", confidence=0.61),
    )
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeSearcher(candidates=candidates))

    handler(_message(text="tarugos"), None, _decision())

    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        (
            "system",
            (
                "Búsqueda en catálogo para «tarugos»: 2 resultado(s): "
                "Tarugo Fischer 8mm (SKU-001), Tarugo Fischer 10mm (SKU-002). "
                "Respondé usando estos productos."
            ),
        ),
        ("user", "tarugos"),
    ]


def test_searcher_database_error_skips_note_and_keeps_reply():
    """Un error de base de datos omite la nota y el responder igual contesta."""
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeSearcher(error=SQLAlchemyError("db down")))

    outcome = handler(_message(text="clavos"), None, _decision())

    assert _roles(fake.calls[0]) == [("system", SYSTEM_PROMPT), ("user", "clavos")]
    assert outcome.reply == "respuesta del modelo"


def test_handler_without_searcher_keeps_slice1_message_shape():
    """Sin searcher, la lista de mensajes mantiene la forma del slice 1: system + historial + usuario."""
    fake = FakeResponder()
    handler = build_handler(fake)

    outcome = handler(_message(text="quiero 10 clavos"), None, _decision())

    assert _roles(fake.calls[0]) == [("system", SYSTEM_PROMPT), ("user", "quiero 10 clavos")]
    assert outcome.reply == "respuesta del modelo"


def test_note_lands_after_history_and_before_latest_user_turn():
    """En una conversación en curso, la nota va después del historial y justo antes del último turno del usuario."""
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeSearcher(candidates=()))
    prev_history = (
        ChatMessage("user", "hola"),
        ChatMessage("assistant", "¿qué andás buscando?"),
    )
    state = ConversationState(sender_id=SENDER, history=prev_history)

    handler(_message(text="10 clavos"), state, _decision())

    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        ("user", "hola"),
        ("assistant", "¿qué andás buscando?"),
        (
            "system",
            (
                "Búsqueda en catálogo para «10 clavos»: sin resultados. "
                "Si el cliente pidió un producto que no está en stock, decíselo al dueño."
            ),
        ),
        ("user", "10 clavos"),
    ]
