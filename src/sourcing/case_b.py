"""Case B: multi-turn supplier selection and PO accumulation.

Per the order-sourcing spec:

- the reply lists each missing item with its candidate suppliers (numbers);
- the owner's selection is persisted on the ``SourcingNeed`` rows (DB source
  of truth — survives the in-memory TTL) and confirmed by accumulating the
  missing items into one OPEN purchase order per selected supplier;
- sourcing is set to IN_PREPARATION the moment the missing items are detected
  (locked decision) and stays there across the selection turns.

``build_sourcing_handler`` wires the SOURCING agent turn: it parses the owner's
numbered reply against the candidates shown in the last reply (recomputed from
the DB by rehydration when the in-memory state expired) and runs
``confirm_selection``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date

from sqlalchemy.orm import Session

from src.channels.base import InboundMessage
from src.db.models import Cliente, Order, SourcingState, SupplierPurchaseOrder
from src.orchestrator.router import AgentOutcome, RoutingDecision
from src.orchestrator.session import ConversationState
from src.purchasing.accumulate import accumulate_need
from src.sourcing.classify import MissingItem
from src.sourcing.persistence import sourcing_needs_for_order, upsert_sourcing_need
from src.supplier.searcher import SupplierCandidate, SupplierCatalogSearcher

_NUMBER_RE = re.compile(r"\d+")


def persist_case_b_order(
    session: Session,
    customer: Cliente,
    *,
    delivery_date: date | None = None,
    missing: tuple[MissingItem, ...],
) -> Order:
    """Persist the order with sourcing IN_PREPARATION and its SourcingNeed rows.

    IN_PREPARATION is set the moment the missing items are detected (not at
    confirmation): the whole Case B span is "in preparation" per the design's
    resolved open question.
    """
    order = Order(
        customer_id=customer.customer_id,
        sourcing_state=SourcingState.IN_PREPARATION,
        delivery_date=delivery_date,
    )
    session.add(order)
    session.flush()
    for item in missing:
        upsert_sourcing_need(session, order.order_id, item.sku, item.missing_quantity)
    session.flush()
    return order


def list_missing_with_suppliers(
    session: Session,
    order: Order,
    searcher: SupplierCatalogSearcher,
) -> tuple[MissingItem, ...]:
    """The order's needs enriched with today's supplier candidates."""
    return tuple(
        MissingItem(
            sku=need.sku,
            description=None,
            requested=need.missing_quantity,
            missing_quantity=need.missing_quantity,
            candidates=searcher.search(sku=need.sku),
        )
        for need in sourcing_needs_for_order(session, order.order_id)
    )


def confirm_selection(
    session: Session,
    order: Order,
    supplier_by_sku: Mapping[str, int],
) -> tuple[SupplierPurchaseOrder, ...]:
    """Accumulate the confirmed selections into one OPEN PO per supplier.

    Each ``sku → supplier_id`` selection accumulates the need into that
    supplier's OPEN purchase order (merging with any existing one) and the
    order stays IN_PREPARATION. Returns the touched purchase orders.
    """
    needs = {need.sku: need for need in sourcing_needs_for_order(session, order.order_id)}
    pos: dict[int, SupplierPurchaseOrder] = {}
    for sku, supplier_id in supplier_by_sku.items():
        need = needs.get(sku)
        if need is None:
            raise KeyError(f"no sourcing need for sku {sku} on order {order.order_id}")
        po = accumulate_need(session, need, supplier_id)
        pos[po.po_id] = po
    order.sourcing_state = SourcingState.IN_PREPARATION
    session.flush()
    return tuple(pos.values())


def format_selection_confirmation(order: Order, pos: tuple[SupplierPurchaseOrder, ...]) -> str:
    """Owner confirmation after the selection is accumulated."""
    po_lines = " ".join(f"PO #{po.po_id} (supplier {po.supplier_id})" for po in pos)
    return (
        f"Listo: pedido #{order.order_id} en preparación. "
        f"{po_lines}. El envío al supplier se ejecuta desde el backoffice."
    )


def parse_supplier_selections(
    text: str, candidates: tuple[SupplierCandidate, ...]
) -> dict[str, int]:
    """Map the owner's numbered reply to per-SKU supplier selections.

    Numbers are 1-based indexes into the candidates shown in the last reply;
    the last number for a SKU wins (re-selection before execution).
    """
    selections: dict[str, int] = {}
    for raw in _NUMBER_RE.findall(text):
        number = int(raw)
        if 1 <= number <= len(candidates):
            candidate = candidates[number - 1]
            selections[candidate.sku] = candidate.supplier_id
    return selections


def build_sourcing_handler(
    session_factory: Callable[[], Session],
) -> Callable[[InboundMessage, ConversationState | None, RoutingDecision], AgentOutcome]:
    """Build the SOURCING agent handler for the Case B confirm flow."""

    def handler(
        message: InboundMessage,
        state: ConversationState | None,
        _decision: RoutingDecision,
    ) -> AgentOutcome:
        if state is None or not state.sourcing_selection_pending:
            return AgentOutcome(state=state, reply="¿Sobre qué pedido querés elegir supplier?")
        selections = parse_supplier_selections(
            (message.text or "").strip(), state.sourcing_candidates
        )
        if not selections:
            return AgentOutcome(
                state=state,
                reply="Respondé el número del supplier que elegís para cada artículo, ej: '1 y 3'.",
            )
        with session_factory() as session:
            order = session.get(Order, state.order_id)
            if order is None:
                return AgentOutcome(state=state, reply="No encuentro ese pedido.")
            pos = confirm_selection(session, order, selections)
            reply = format_selection_confirmation(order, pos)
            session.commit()
        # The selection phase stays open until the POs are executed (SENT) in
        # the backoffice, so the owner can still re-select before execution.
        updated = state.with_updates(sourcing_selection_pending=True)
        return AgentOutcome(state=updated, reply=reply)

    return handler
