"""Sourcing router tests (task 3.5).

Routing additions for the sourcing workflow: a reply while the owner is
selecting suppliers goes to the SOURCING confirm flow, and pending decisions
keep their precedence rules (awaiting-decision replies go to Dispatch first).
"""

from __future__ import annotations

from src.channels.base import InboundMessage
from src.orchestrator.router import AgentName, route_message
from src.orchestrator.session import ConversationState


def _message(
    *, text: str | None = None, media_type: str | None = None, sender: str = "+5491155551234"
) -> InboundMessage:
    return InboundMessage(channel="whatsapp", sender_id=sender, text=text, media_type=media_type)


def _state(sender: str = "+5491155551234", **overrides) -> ConversationState:
    defaults = {"sender_id": sender}
    defaults.update(overrides)
    return ConversationState(**defaults)


def test_supplier_selection_reply_routes_to_sourcing():
    """A reply during supplier selection routes to the SOURCING flow."""
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


def test_draft_carrying_state_routes_to_customer_before_sales_or_disambiguation():
    """A product-selection draft owns the next text turn before an order exists."""
    state = _state(order_id=None, draft_items=((object(), 1),))

    decision = route_message(_message(text="cerrá el pedido para Cliente"), state)

    assert decision.agent is AgentName.CUSTOMER
    assert decision.context_loaded is True


def test_fresh_text_routes_to_customer_without_a_parse_step():
    """Texto fresco va al Customer agent: no hay paso de parseo."""
    decision = route_message(_message(text="quiero 10 clavos"), None)
    assert decision.agent is AgentName.CUSTOMER
