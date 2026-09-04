"""Customer conversational handler tests (rag-product-query W3).

The Customer agent answers through a mockable LLM responder and injects a
transient, source-aware product-context note (via a fake ``ProductSearcher``)
before the user turn: LOCAL/RAG results list the products, NONE asks for a
synonym/reformulation, ERROR reports the catalogs could not be consulted —
never a stock claim. Add-intent phrases ("agregalo", "sumá 5 de eso", "el 2")
short-circuit the LLM and accumulate into the state's ``draft_items`` when an
order is open, or offer to create one otherwise. Every test uses fakes — no
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
    format_added_to_order_reply,
)
from src.agents.product_search import (
    ProductEntry,
    ProductSearchResult,
    ProductSource,
)
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


class FakeProductSearcher:
    """Deterministic fake product searcher: configured result, can raise.

    ``by_query`` maps specific queries to results; any other query falls back
    to ``result`` (or a NONE result when no result is configured).
    """

    def __init__(
        self,
        result: ProductSearchResult | None = None,
        error: Exception | None = None,
        by_query: dict[str, ProductSearchResult] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.by_query = by_query or {}
        self.queries: list[str] = []

    def search(self, query: str) -> ProductSearchResult:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        if query in self.by_query:
            return self.by_query[query]
        return (
            self.result
            if self.result is not None
            else ProductSearchResult(source=ProductSource.NONE)
        )


def _entry(
    sku: str,
    name: str,
    *,
    source: ProductSource = ProductSource.LOCAL,
    price: float | None = None,
    currency: str | None = None,
    unit: str | None = None,
    brand: str | None = None,
    provider: str | None = None,
    specs: str | None = None,
    source_file: str | None = None,
    page: int | None = None,
) -> ProductEntry:
    return ProductEntry(
        sku=sku,
        name=name,
        source=source,
        price=price,
        currency=currency,
        unit=unit,
        brand=brand,
        provider=provider,
        specs=specs,
        source_file=source_file,
        page=page,
    )


def _local_result(*entries: ProductEntry) -> ProductSearchResult:
    return ProductSearchResult(source=ProductSource.LOCAL, entries=entries)


def _rag_result(*entries: ProductEntry) -> ProductSearchResult:
    return ProductSearchResult(source=ProductSource.RAG, entries=entries)


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


NONE_NOTE = (
    "Catalog results for «¿tienen tarugos fisher?»: no match in current catalogs. "
    "Ask the owner for a synonym or reformulation. "
    "Do not claim the item is out of stock."
)


def test_product_query_with_none_result_injects_not_found_note():
    """Un resultado NONE inyecta la nota de no encontrado y el historial no la guarda."""
    fake = FakeResponder()
    searcher = FakeProductSearcher()
    handler = build_handler(fake, searcher=searcher)

    outcome = handler(_message(text="¿tienen tarugos fisher?"), None, _decision())

    assert searcher.queries == ["¿tienen tarugos fisher?"]
    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        ("system", NONE_NOTE),
        ("user", "¿tienen tarugos fisher?"),
    ]
    assert outcome.state is not None
    assert [m.role for m in outcome.state.history] == ["user", "assistant"]
    assert outcome.state.product_options == ()


def test_local_candidates_become_note_listing_names_and_skus():
    """Un resultado LOCAL lista nombre oficial y SKU de cada producto bajo own stock."""
    entries = (
        _entry("SKU-001", "Tarugo Fischer 8mm"),
        _entry("SKU-002", "Tarugo Fischer 10mm"),
    )
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeProductSearcher(result=_local_result(*entries)))

    outcome = handler(_message(text="tarugos"), None, _decision())

    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        (
            "system",
            (
                "Catalog results for «tarugos» — own stock:\n"
                "1. Tarugo Fischer 8mm (SKU-001)\n"
                "2. Tarugo Fischer 10mm (SKU-002)"
            ),
        ),
        ("user", "tarugos"),
    ]
    assert outcome.state is not None
    assert outcome.state.product_options == entries


def test_rag_results_note_numbered_cheapest_first_with_fields_and_footer():
    """Un resultado RAG se numera, ordena por precio ascendente e incluye campos y footer."""
    expensive = _entry(
        "AMX-AT-5044",
        "Tarugo Fischer 8mm",
        source=ProductSource.RAG,
        brand="Fischer",
        provider="AMX",
        price=180.0,
        currency="ARS",
        unit="bolsa",
        specs="plástico, 8mm",
        source_file="catalogo-2024.pdf",
        page=12,
    )
    cheap = _entry(
        "AMX-AT-5045",
        "Tarugo Fischer 10mm",
        source=ProductSource.RAG,
        brand="Fischer",
        provider="AMX",
        price=135.5,
        currency="ARS",
        unit="bolsa",
        specs="plástico, 10mm",
        source_file="catalogo-2024.pdf",
        page=13,
    )
    fake = FakeResponder()
    # Input out of order on purpose: the note must sort cheapest first.
    handler = build_handler(
        fake, searcher=FakeProductSearcher(result=_rag_result(expensive, cheap))
    )

    handler(_message(text="tarugos"), None, _decision())

    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        (
            "system",
            (
                "Catalog results for «tarugos» — supplier catalog:\n"
                "1. Tarugo Fischer 10mm — Fischer — AMX — 135.5 ARS/bolsa — "
                "plástico, 10mm — catalogo-2024.pdf p.13\n"
                "2. Tarugo Fischer 8mm — Fischer — AMX — 180 ARS/bolsa — "
                "plástico, 8mm — catalogo-2024.pdf p.12\n"
                "These are supplier-catalog items, not own stock."
            ),
        ),
        ("user", "tarugos"),
    ]


def test_rag_note_never_leaks_raw_double_prefix_codigo():
    """La nota RAG nunca muestra el codigo crudo con doble prefijo (higiene de SKU)."""
    entry = _entry(
        "AMX-AT-5044",
        "Tarugo Fischer 8mm",
        source=ProductSource.RAG,
        price=100.0,
    )
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeProductSearcher(result=_rag_result(entry)))

    handler(_message(text="tarugos"), None, _decision())

    note = fake.calls[0][1].content
    assert "AMX-AMX-AT-5044" not in note


def test_add_intent_preserves_normalized_rag_sku_in_draft():
    """El intent de alta conserva el SKU normalizado del producto RAG en el draft."""
    fake = FakeResponder()
    entry = _entry(
        "AMX-AT-5044",
        "Tarugo Fischer 8mm",
        source=ProductSource.RAG,
        price=135.5,
    )
    state = ConversationState(sender_id=SENDER, order_id=5, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="agregalo"), state, _decision())

    assert fake.calls == []
    assert outcome.state is not None
    assert outcome.state.draft_items == ((entry, 1),)
    assert outcome.state.draft_items[0][0].sku == "AMX-AT-5044"


def test_refusal_none_note_suggests_reformulation_without_stock_claim():
    """La nota NONE sugiere sinónimos/reformulación y no afirma estado de stock."""
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeProductSearcher())

    handler(_message(text="¿tienen tarugos fisher?"), None, _decision())

    note = fake.calls[0][1].content
    assert "no match in current catalogs" in note
    assert "synonym or reformulation" in note
    assert "no está en stock" not in note
    assert "sin stock" not in note


def test_error_note_states_catalogs_unavailable_without_stock_claim():
    """La nota ERROR dice que los catálogos no pudieron consultarse, sin stock claim."""
    fake = FakeResponder()
    searcher = FakeProductSearcher(result=ProductSearchResult(source=ProductSource.ERROR))
    handler = build_handler(fake, searcher=searcher)

    outcome = handler(_message(text="tarugos"), None, _decision())

    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        (
            "system",
            (
                "Catalog results for «tarugos»: supplier catalogs could not be "
                "consulted. Tell the owner they are unavailable and offer to retry "
                "later. Do not claim the item is out of stock."
            ),
        ),
        ("user", "tarugos"),
    ]
    assert outcome.state is not None
    assert outcome.state.product_options == ()


def test_dual_source_note_lists_local_first_labeled():
    """Un draft mixto (local + RAG) renderiza local primero, numeración global y etiquetado."""
    local_entry = _entry("SKU-001", "Tarugo Fischer 8mm", source=ProductSource.LOCAL)
    rag_entry = _entry(
        "AMX-AT-5044",
        "Tarugo Fischer 8mm",
        source=ProductSource.RAG,
        price=135.5,
        currency="ARS",
        unit="bolsa",
        source_file="catalogo-2024.pdf",
        page=3,
    )
    fake = FakeResponder()
    state = ConversationState(
        sender_id=SENDER,
        order_id=7,
        draft_items=((rag_entry, 2),),
    )
    handler = build_handler(fake, searcher=FakeProductSearcher(result=_local_result(local_entry)))

    handler(_message(text="tarugos"), state, _decision())

    assert _roles(fake.calls[0]) == [
        ("system", SYSTEM_PROMPT),
        (
            "system",
            (
                "Catalog results for «tarugos» — own stock (local):\n"
                "1. Tarugo Fischer 8mm (SKU-001) [local]\n"
                "Catalog results for «tarugos» — supplier catalog (rag):\n"
                "2. Tarugo Fischer 8mm — 135.5 ARS/bolsa — catalogo-2024.pdf p.3 [rag]"
            ),
        ),
        ("user", "tarugos"),
    ]


def test_searcher_database_error_skips_note_and_keeps_reply():
    """Un error de base de datos omite la nota y el responder igual contesta."""
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeProductSearcher(error=SQLAlchemyError("db down")))

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
    handler = build_handler(fake, searcher=FakeProductSearcher())
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
                "Catalog results for «10 clavos»: no match in current catalogs. "
                "Ask the owner for a synonym or reformulation. "
                "Do not claim the item is out of stock."
            ),
        ),
        ("user", "10 clavos"),
    ]


def test_add_intent_with_open_order_appends_draft_and_clears_options():
    """'agregalo' con pedido abierto agrega el producto al draft sin llamar al LLM."""
    fake = FakeResponder()
    entry = _entry("SKU-001", "Tarugo Fischer 8mm")
    state = ConversationState(sender_id=SENDER, order_id=5, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="agregalo"), state, _decision())

    assert fake.calls == []
    assert outcome.reply == format_added_to_order_reply(entry, 1)
    assert outcome.state is not None
    assert outcome.state.draft_items == ((entry, 1),)
    assert outcome.state.product_options == ()
    assert outcome.state.history == (
        ChatMessage("user", "agregalo"),
        ChatMessage("assistant", outcome.reply),
    )


def test_add_intent_with_quantity_appends_draft_with_qty():
    """'sumá 5 de eso' agrega 5 unidades del último producto mostrado al draft."""
    fake = FakeResponder()
    entry = _entry("SKU-001", "Tarugo Fischer 8mm")
    state = ConversationState(sender_id=SENDER, order_id=5, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="sumá 5 de eso"), state, _decision())

    assert fake.calls == []
    assert outcome.state is not None
    assert outcome.state.draft_items == ((entry, 5),)


def test_add_intent_numbered_reference_picks_displayed_result():
    """'el 2' selecciona el segundo resultado mostrado."""
    fake = FakeResponder()
    first = _entry("SKU-001", "Tarugo Fischer 8mm")
    second = _entry("SKU-002", "Tarugo Fischer 10mm")
    state = ConversationState(sender_id=SENDER, order_id=5, product_options=(first, second))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="el 2"), state, _decision())

    assert fake.calls == []
    assert outcome.state is not None
    assert outcome.state.draft_items == ((second, 1),)


def test_add_intent_without_open_order_starts_draft():
    """An add intent starts the draft even when no persisted order exists yet."""
    fake = FakeResponder()
    entry = _entry("SKU-001", "Tarugo Fischer 8mm")
    state = ConversationState(sender_id=SENDER, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="agregalo"), state, _decision())

    assert fake.calls == []
    assert outcome.reply == format_added_to_order_reply(entry, 1)
    assert outcome.state is not None
    assert outcome.state.draft_items == ((entry, 1),)
    assert outcome.state.product_options == ()


def test_bare_quantity_after_displayed_product_adds_qty_with_finalize_hint():
    """'quiero 2' after a displayed product adds 2 of it with the finalize hint, no LLM."""
    fake = FakeResponder()
    entry = _entry("SKU-001", "Tarugo Fischer 8mm")
    state = ConversationState(sender_id=SENDER, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="quiero 2"), state, _decision())

    assert fake.calls == []
    assert outcome.reply == format_added_to_order_reply(entry, 2)
    assert "cerrá el pedido para" in outcome.reply
    assert outcome.state is not None
    assert outcome.state.draft_items == ((entry, 2),)
    assert outcome.state.product_options == ()
    assert outcome.state.history == (
        ChatMessage("user", "quiero 2"),
        ChatMessage("assistant", outcome.reply),
    )


def test_add_intent_reply_shows_rag_price_currency_and_unit():
    """Un alta de un producto RAG con precio muestra el precio en la respuesta."""
    fake = FakeResponder()
    entry = _entry(
        "AMX-AT-5044",
        "Tarugo Fischer 8mm",
        source=ProductSource.RAG,
        price=135.5,
        currency="ARS",
        unit="bolsa",
    )
    state = ConversationState(sender_id=SENDER, order_id=5, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="agregalo"), state, _decision())

    assert fake.calls == []
    assert outcome.reply == format_added_to_order_reply(entry, 1)
    assert "(135.5 ARS/bolsa)" in outcome.reply


def test_add_intent_reply_omits_price_for_local_entry_without_price():
    """Un alta de un producto local sin precio no muestra ningún precio."""
    fake = FakeResponder()
    entry = _entry("SKU-001", "Tarugo Fischer 8mm")
    state = ConversationState(sender_id=SENDER, order_id=5, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="agregalo"), state, _decision())

    assert fake.calls == []
    assert outcome.reply == format_added_to_order_reply(entry, 1)
    assert "(" not in outcome.reply


def test_remove_command_removes_in_memory_draft_line():
    """'sacá X' quita la línea del draft en memoria sin llamar al LLM."""
    fake = FakeResponder()
    nails = _entry("SKU-001", "Tarugo Fischer 8mm")
    paint = _entry("SKU-002", "Pintura Látex Blanco")
    state = ConversationState(
        sender_id=SENDER,
        order_id=5,
        draft_items=((nails, 2), (paint, 1)),
    )
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="sacá la pintura"), state, _decision())

    assert fake.calls == []
    assert outcome.state is not None
    assert outcome.state.draft_items == ((nails, 2),)  # only the matched line left
    assert "Pintura Látex Blanco" in outcome.reply  # type: ignore[operator]


def test_remove_command_unknown_target_reports_without_touching_draft():
    """Un objetivo que no está en el draft informa y no modifica nada."""
    fake = FakeResponder()
    state = ConversationState(
        sender_id=SENDER,
        order_id=5,
        draft_items=((_entry("SKU-001", "Tarugo Fischer 8mm"), 2),),
    )
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="sacá la pintura"), state, _decision())

    assert fake.calls == []
    assert outcome.state is not None
    assert outcome.state.draft_items == state.draft_items
    assert "No encontré ese artículo" in outcome.reply  # type: ignore[operator]


def test_add_phrase_without_options_goes_to_llm():
    """Sin opciones mostradas, la frase de alta no es intent y sigue el camino LLM."""
    fake = FakeResponder()
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="agregalo"), None, _decision())

    assert len(fake.calls) == 1
    assert outcome.reply == "respuesta del modelo"


def test_query_updates_product_options_for_next_turn():
    """Un query con resultados deja product_options listas para la referencia del próximo turno."""
    fake = FakeResponder()
    entries = (_entry("SKU-001", "Tarugo Fischer 8mm"),)
    handler = build_handler(fake, searcher=FakeProductSearcher(result=_local_result(*entries)))

    outcome = handler(_message(text="tarugos"), None, _decision())

    assert outcome.state is not None
    assert outcome.state.product_options == entries


def test_verb_quantity_add_after_displayed_product_adds_qty():
    """'agregale 2' after a displayed product adds 2 of it without calling the LLM."""
    fake = FakeResponder()
    entry = _entry("SKU-001", "Tarugo Fischer 8mm")
    state = ConversationState(sender_id=SENDER, order_id=5, product_options=(entry,))
    handler = build_handler(fake, searcher=FakeProductSearcher())

    outcome = handler(_message(text="agregale 2"), state, _decision())

    assert fake.calls == []
    assert outcome.reply == format_added_to_order_reply(entry, 2)
    assert outcome.state is not None
    assert outcome.state.draft_items == ((entry, 2),)
    assert outcome.state.product_options == ()


def test_turn_without_results_keeps_last_displayed_product_anchor():
    """A turn whose search shows nothing keeps product_options, so a later bare '2' still adds."""
    fake = FakeResponder()
    entry = _entry("SKU-001", "Tarugo Fischer 8mm")
    searcher = FakeProductSearcher(
        by_query={
            "tarugos": _local_result(entry),
            "dale": ProductSearchResult(source=ProductSource.NONE, entries=()),
        }
    )
    handler = build_handler(fake, searcher=searcher)

    displayed = handler(_message(text="tarugos"), None, _decision())
    assert displayed.state is not None
    assert displayed.state.product_options == (entry,)

    nothing = handler(_message(text="dale"), displayed.state, _decision())
    assert nothing.state is not None
    assert nothing.state.product_options == (entry,)

    added = handler(_message(text="2"), nothing.state, _decision())
    assert fake.calls  # the "dale" turn went through the LLM
    assert added.state is not None
    assert added.state.draft_items == ((entry, 2),)
    assert added.state.product_options == ()
