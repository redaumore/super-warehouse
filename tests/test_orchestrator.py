"""Orchestrator tests (task 2.8).

Routing is a pure decision over the message shape + conversation state; the
Orchestrator wires routing to the session store and preserves context across
agents. The agent-orchestration spec scenarios covered:

- voice note → Perception (transcribe); barcode/photo → Perception (vision);
- owner decision → Dispatch, resuming the order awaiting the decision;
- multi-step order preserves customer/order/items context between agents.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.channels.base import InboundMessage
from src.orchestrator.router import AgentName, Orchestrator, route_message
from src.orchestrator.session import ConversationState, ConversationStore, ResolvedItem


def _message(
    *,
    text: str | None = None,
    media_type: str | None = None,
    sender: str = "+5491155551234",
) -> InboundMessage:
    return InboundMessage(
        channel="whatsapp",
        sender_id=sender,
        text=text,
        media_type=media_type,
    )


def _state(sender: str = "+5491155551234", **overrides) -> ConversationState:
    defaults = {"sender_id": sender}
    defaults.update(overrides)
    return ConversationState(**defaults)


# ------------------------------------------------------------- routing rules


def test_voice_note_routes_to_perception_stt():
    """Una nota de voz se enruta a Percepción (transcripción)."""
    decision = route_message(_message(media_type="voice"), None)
    assert decision.agent is AgentName.PERCEPTION
    assert decision.media_kind == "voice"


def test_image_routes_to_perception_vision():
    """Una imagen (foto de remito/código) se enruta a Percepción (visión).

    Barcode/remito photos go to perception, never to the order-intake path.
    """
    decision = route_message(_message(media_type="image"), None)
    assert decision.agent is AgentName.PERCEPTION
    assert decision.media_kind == "image"


def test_fresh_text_routes_to_customer():
    """Un texto nuevo de cliente se enruta a Customer."""
    decision = route_message(_message(text="quiero 10 clavos"), None)
    assert decision.agent is AgentName.CUSTOMER


def test_owner_approval_routes_to_dispatch_resuming_order():
    """La aprobación del dueño se enruta a Despacho reanudando el pedido."""
    state = _state(order_id=7, awaiting_decision=True)
    decision = route_message(_message(text="sí, aprobá"), state)
    assert decision.agent is AgentName.DISPATCH
    assert decision.context_loaded is True


def test_owner_rejection_routes_to_dispatch():
    """El rechazo del dueño se enruta a Despacho."""
    state = _state(order_id=7, awaiting_decision=True)
    decision = route_message(_message(text="no, rechazá"), state)
    assert decision.agent is AgentName.DISPATCH


def test_non_decision_reply_while_awaiting_goes_to_dispatch_menu():
    """Una respuesta ambigua mientras se espera sigue en la conversación del dueño.

    An ambiguous owner reply still belongs to the owner conversation.
    """
    state = _state(order_id=7, awaiting_decision=True)
    decision = route_message(_message(text="hablamos mañana"), state)
    assert decision.agent is AgentName.DISPATCH


def test_in_progress_order_with_items_routes_to_sales():
    """Un pedido en curso con ítems se enruta a Ventas."""
    state = _state(
        order_id=7,
        items=(ResolvedItem(sku="CLV-001", cantidad=10),),
        awaiting_decision=False,
    )
    decision = route_message(_message(text="agregá 2 más"), state)
    assert decision.agent is AgentName.SALES


def test_in_progress_order_without_items_routes_to_disambiguation():
    """Un pedido en curso sin ítems se enruta a Desambiguación."""
    state = _state(order_id=7, items=(), awaiting_decision=False)
    decision = route_message(_message(text="el otro clavito"), state)
    assert decision.agent is AgentName.DISAMBIGUATION


def test_textless_medialess_message_routes_to_customer():
    """Un mensaje sin texto ni media se enruta a Customer."""
    decision = route_message(_message(text=None, media_type=None), None)
    assert decision.agent is AgentName.CUSTOMER


# ------------------------------------------------------------- session store


def test_store_preserves_context_between_steps():
    """El almacén de conversación conserva el contexto entre pasos."""
    store = ConversationStore()
    first = _state(customer_id=3, order_id=7)
    store.put(first)
    loaded = store.get("+5491155551234")
    assert loaded is not None
    assert loaded.customer_id == 3
    assert loaded.order_id == 7


def test_store_drops_expired_context():
    """El almacén descarta el contexto vencido por TTL."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    store = ConversationStore(now=lambda: now)
    stale = _state(order_id=7)
    stale.updated_at = now - timedelta(minutes=31)  # TTL is 30 minutes
    store.put(stale)
    assert store.get("+5491155551234") is None


def test_store_keeps_fresh_context():
    """El almacén conserva el contexto reciente."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    store = ConversationStore(now=lambda: now)
    fresh = _state(order_id=7)
    fresh.updated_at = now - timedelta(minutes=5)
    store.put(fresh)
    assert store.get("+5491155551234") is not None


def test_store_drop_removes_context():
    """Eliminar el contexto lo borra del almacén."""
    store = ConversationStore()
    store.put(_state(order_id=7))
    store.drop("+5491155551234")
    assert store.get("+5491155551234") is None


def test_with_updates_returns_new_state_and_touches_clock():
    """Actualizar devuelve un estado nuevo y refresca el reloj."""
    state = _state(customer_id=3)
    updated = state.with_updates(order_id=7)
    assert updated.order_id == 7
    assert updated.customer_id == 3  # untouched fields preserved
    assert updated is not state


# ------------------------------------------------------------ orchestrator


def _capturing_handler(updated_state):
    """Handler factory recording invocations for assertions."""

    def handler(message, state, decision):
        calls.append((message, state, decision))
        return updated_state

    calls: list = []
    return handler, calls


def test_orchestrator_routes_and_persists_context():
    """El orquestador enruta y persiste el contexto."""
    store = ConversationStore()
    handler, calls = _capturing_handler(_state(sender_id="+5491155551234", customer_id=3, order_id=7))
    orchestrator = Orchestrator(store, agents={AgentName.CUSTOMER: handler})

    decision = orchestrator.handle_inbound(_message(text="quiero 10 clavos"))

    assert decision.agent is AgentName.CUSTOMER
    assert len(calls) == 1
    assert calls[0][1] is None  # no prior context on first message
    assert store.get("+5491155551234") is not None  # context persisted


def test_orchestrator_resumes_order_after_owner_wait():
    """Tras la espera del dueño, su respuesta reanuda el mismo pedido.

    Human-in-the-loop: the owner's later reply resumes the same order.
    """
    store = ConversationStore()
    store.put(_state(sender_id="+5491155551234", customer_id=3, order_id=7, awaiting_decision=True))
    dispatch_handler, dispatch_calls = _capturing_handler(
        _state(sender_id="+5491155551234", order_id=7, awaiting_decision=False)
    )
    orchestrator = Orchestrator(store, agents={AgentName.DISPATCH: dispatch_handler})

    decision = orchestrator.handle_inbound(_message(text="sí, aprobá"))

    assert decision.agent is AgentName.DISPATCH
    assert dispatch_calls[0][1].order_id == 7  # context was loaded, not lost
    assert store.get("+5491155551234").awaiting_decision is False  # updated


def test_orchestrator_register_binds_handler():
    """Registrar un agente enlaza su handler."""
    store = ConversationStore()
    handler, _ = _capturing_handler(_state(sender_id="x"))
    orchestrator = Orchestrator(store)
    orchestrator.register(AgentName.PERCEPTION, handler)
    assert orchestrator.agents[AgentName.PERCEPTION] is handler
