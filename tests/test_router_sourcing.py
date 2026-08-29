"""Sourcing router tests (task 3.5).

Routing additions for the sourcing workflow: a reply while the owner is
selecting suppliers goes to the SOURCING confirm flow, and the orchestrator's
parse step extracts structured order fields before a fresh text reaches the
Customer agent (routing a parsed turn to it with ``parsed=True``).
"""

from __future__ import annotations

from src.agents.intake import SimpleOrderParser
from src.channels.base import InboundMessage
from src.orchestrator.router import AgentName, AgentOutcome, Orchestrator, route_message
from src.orchestrator.session import ConversationState, ConversationStore


def _message(
    *, text: str | None = None, media_type: str | None = None, sender: str = "+5491155551234"
) -> InboundMessage:
    return InboundMessage(channel="whatsapp", sender_id=sender, text=text, media_type=media_type)


def _state(sender: str = "+5491155551234", **overrides) -> ConversationState:
    defaults = {"sender_id": sender}
    defaults.update(overrides)
    return ConversationState(**defaults)


def test_supplier_selection_reply_routes_to_sourcing():
    """Una respuesta durante la selección de proveedor va al flujo SOURCING."""
    state = _state(
        order_id=7,
        sourcing_selection_pending=True,
        sourcing_needs=(),
    )
    decision = route_message(_message(text="elijo el 1"), state)
    assert decision.agent is AgentName.SOURCING
    assert decision.context_loaded is True


def test_approval_decision_still_routes_to_dispatch_first():
    """Una decisión de aprobación pendiente sigue yendo a Dispatch."""
    state = _state(order_id=7, awaiting_decision=True, sourcing_selection_pending=True)
    decision = route_message(_message(text="aprobá"), state)
    assert decision.agent is AgentName.DISPATCH


def test_customer_disambiguation_pending_routes_to_customer():
    """Un cliente ambiguo pendiente de elegir sigue yendo al Customer agent."""
    state = _state(
        sender="+5491100000000",
        customer_disambiguation_pending=True,
        customer_candidates=(),
    )
    decision = route_message(_message(text="1", sender="+5491100000000"), state)
    assert decision.agent is AgentName.CUSTOMER
    assert decision.context_loaded is True


def test_fresh_text_without_pending_selection_routes_to_customer():
    """Texto fresco sin selección pendiente sigue yendo a Customer."""
    decision = route_message(_message(text="quiero 10 clavos"), None)
    assert decision.agent is AgentName.CUSTOMER


def test_parse_step_extracts_order_before_customer_agent():
    """El paso de parseo extrae la orden antes de llegar al Customer agent."""
    store = ConversationStore()
    seen: dict[str, object] = {}

    def customer_handler(message, state, decision):
        seen["state"] = state
        seen["decision"] = decision
        return AgentOutcome(state=state.with_updates(), reply="ok")

    orchestrator = Orchestrator(
        store, agents={AgentName.CUSTOMER: customer_handler}, parser=SimpleOrderParser()
    )
    result = orchestrator.handle_inbound(_message(text="quiero 10 clavos para el viernes"))

    assert result.decision.agent is AgentName.CUSTOMER
    assert result.decision.parsed is True
    state = seen["state"]
    assert state is not None
    assert state.parsed_order is not None
    assert len(state.parsed_order.items) == 1
    assert state.parsed_order.items[0].description == "clavos"
    assert state.parsed_order.delivery_date is not None


def test_parse_step_skips_non_order_messages():
    """Un saludo no se parsea: se mantiene el chat conversacional."""
    store = ConversationStore()
    seen: dict[str, object] = {}

    def customer_handler(message, state, decision):
        seen["state"] = state
        seen["decision"] = decision
        return AgentOutcome(state=ConversationState(sender_id=message.sender_id), reply="hola")

    orchestrator = Orchestrator(
        store, agents={AgentName.CUSTOMER: customer_handler}, parser=SimpleOrderParser()
    )
    result = orchestrator.handle_inbound(_message(text="hola que tal"))

    assert result.decision.parsed is False
    assert seen["state"] is None  # no parsed context was attached


def test_parse_step_off_when_no_parser_wired():
    """Sin parser no hay paso de parseo (routing legacy intacto)."""
    store = ConversationStore()
    seen: dict[str, object] = {}

    def customer_handler(message, state, decision):
        seen["decision"] = decision
        return AgentOutcome(state=ConversationState(sender_id=message.sender_id))

    orchestrator = Orchestrator(store, agents={AgentName.CUSTOMER: customer_handler})
    result = orchestrator.handle_inbound(_message(text="quiero 10 clavos"))
    assert result.decision.parsed is False
    assert seen["decision"].agent is AgentName.CUSTOMER


def test_parse_step_does_not_override_in_progress_orders():
    """Un pedido en curso (estado cargado) no se re-parsea."""
    store = ConversationStore()
    store.put(_state(order_id=7, items=(), sourcing_selection_pending=False))
    seen: dict[str, object] = {}

    def disambiguation_handler(message, state, decision):
        seen["decision"] = decision
        return AgentOutcome(state=state.with_updates())

    orchestrator = Orchestrator(
        store, agents={AgentName.DISAMBIGUATION: disambiguation_handler}, parser=SimpleOrderParser()
    )
    result = orchestrator.handle_inbound(_message(text="el otro clavito"))
    assert result.decision.agent is AgentName.DISAMBIGUATION
    assert result.decision.parsed is False
