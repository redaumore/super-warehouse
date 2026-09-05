"""Dispatch agent: confirm/cancel parsing and the wired confirm flow.

Owns the owner-facing decision tools of the pipeline:

- ``parse_decision`` — interprets the owner's reply ("sí, aprobá" vs
  "no, rechazá", optionally with per-line adjustments like "hacé un 5% de
  descuento extra en clavos");
- ``parse_order_reference`` — extracts an explicit ``pedido #N`` reference so
  a decision can target a specific order instead of the rehydrated latest one;
- ``apply_decision`` — applies the decision to the order through the lifecycle:
  APPROVE applies the per-line adjustments (the confirm ceremony runs the
  transition + registration), REJECT cancels the order (``cancel_order`` with
  the owner as actor);
- ``build_dispatch_handler`` — the wired DISPATCH agent turn: parse decision →
  load order (``#N`` override or the conversation's ``order_id``) →
  ``apply_decision`` + ``confirm_and_register`` (classify + convert + deduct +
  Sheets). A Sheets quarantine is tolerated: the order stays CONFIRMED and the
  failure is surfaced in the in-chat reply. A stale quote raises
  ``RequiresRequoteError`` and rolls back.

Decision parsing is pure; the handler is built with injectable boundaries
(session factory + ``SheetsWriter`` + optional supplier searcher) so unit
tests never touch the network. The old ``owner_phone`` push
(``notify_owner``) is gone — confirmations, cancellations and errors are
in-chat replies.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.disambiguation import normalize_text
from src.agents.sales import Quote
from src.channels.base import InboundMessage
from src.db.models import Order, OrderItem
from src.integrations.sheets import SheetsWriter
from src.observability.session_logger import log_session_event
from src.orchestrator.approval import PendingConversionError, confirm_and_register
from src.orchestrator.router import AgentOutcome, RoutingDecision
from src.orchestrator.session import ConversationState, SourcingNeedItem
from src.order_lifecycle.state import (
    InvalidTransitionError,
    RequiresRequoteError,
    cancel_order,
)
from src.supplier.searcher import SupplierCatalogSearcher

_CENT = Decimal("0.01")

_APPROVE_RE = re.compile(r"\b(aprob|dale|s[ií]|ok|confirm|acept|adelante)", re.IGNORECASE)
_REJECT_RE = re.compile(r"\b(rechaz|nop|negativ|no(?!\w))", re.IGNORECASE)
_ADJUST_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%\s*(?:extra\s+)?(?:de\s+)?descuento\s+(?:extra\s+)?(?:en\s+)?([^.,;]+)",
    re.IGNORECASE,
)
_ORDER_REF_RE = re.compile(r"#\s*(\d+)")


class UnknownDecisionError(Exception):
    """The owner's reply could not be resolved to confirm or cancel."""


class UnknownAdjustmentTargetError(Exception):
    """An adjustment names a line that does not exist in the order/quote."""


@dataclass(frozen=True)
class LineAdjustment:
    """An owner adjustment: extra discount percentage on one line."""

    sku: str
    extra_discount_pct: Decimal


class DecisionAction(str, enum.Enum):
    """Owner decision outcome: confirm, cancel, or unresolved."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Decision:
    """Parsed owner reply: an action plus optional per-line adjustments."""

    action: DecisionAction
    adjustments: tuple[LineAdjustment, ...] = ()


def format_quote_message(quote: Quote, order_id: int, customer_name: str | None = None) -> str:
    """Render a quote as a WhatsApp-ready owner message."""
    who = f" ({customer_name})" if customer_name else ""
    lines = [
        f"Pedido #{order_id}{who} — {quote.total:.2f} {quote.currency}",
    ]
    for line in quote.lines:
        suffix = f" (descuento {line.adjustment:.2f})" if line.adjustment else ""
        lines.append(
            f"- {line.cantidad} × {line.description or line.sku}: {line.final_price:.2f}{suffix}"
        )
    lines.append("Respondé 'aprobá' o 'rechazá' (podés agregar descuentos: '5% extra en clavos').")
    return "\n".join(lines)


def parse_order_reference(text: str) -> int | None:
    """Extract an explicit ``pedido #N`` / ``#N`` reference from the reply.

    Returns ``None`` when no numbered reference is present; the caller then
    targets the conversation's rehydrated order (latest open).
    """
    match = _ORDER_REF_RE.search(text or "")
    return int(match.group(1)) if match else None


def parse_decision(text: str) -> Decision:
    """Resolve an owner reply to confirm / cancel (+ per-line adjustments)."""
    if not text or not text.strip():
        return Decision(action=DecisionAction.UNKNOWN)
    raw = text.strip()
    adjustments = tuple(
        LineAdjustment(
            sku=target.strip(),
            # "5%" is a percent; pricing math consumes a fraction (0.05).
            extra_discount_pct=Decimal(pct.replace(",", ".")) / Decimal(100),
        )
        for pct, target in _ADJUST_RE.findall(raw)
    )
    if _REJECT_RE.search(raw):
        return Decision(action=DecisionAction.REJECT)
    if _APPROVE_RE.search(raw):
        return Decision(action=DecisionAction.APPROVE, adjustments=adjustments)
    return Decision(action=DecisionAction.UNKNOWN)


def _resolve_adjustment_sku(quote: Quote | None, target: str) -> str:
    """Map the owner's product phrase to a SKU via the quote's descriptions."""
    if quote is None:
        return target  # caller passes SKUs directly when no quote is available
    needle = normalize_text(target)
    for line in quote.lines:
        if line.sku == target or (line.description and needle in normalize_text(line.description)):
            return line.sku
    raise UnknownAdjustmentTargetError(f"no line matches adjustment target: {target}")


def _apply_line_adjustments(
    session: Session, order: Order, decision: Decision, quote: Quote | None
) -> None:
    """Re-price the affected order_items rows by each extra discount."""
    items = {
        item.sku: item
        for item in session.scalars(select(OrderItem).where(OrderItem.order_id == order.order_id))
    }
    for adjustment in decision.adjustments:
        sku = _resolve_adjustment_sku(quote, adjustment.sku)
        item = items.get(sku)
        if item is None:
            raise UnknownAdjustmentTargetError(f"sku not in order: {sku}")
        pct = Decimal(adjustment.extra_discount_pct)
        new_final = (item.final_price * (Decimal(1) - pct)).quantize(_CENT, rounding=ROUND_HALF_UP)
        item.adjustment = (item.final_price - new_final).quantize(_CENT, rounding=ROUND_HALF_UP)
        item.final_price = new_final


def apply_decision(
    session: Session,
    order: Order,
    decision: Decision,
    *,
    quote: Quote | None = None,
    now: datetime | None = None,
) -> Order:
    """Apply the owner's decision to the order.

    APPROVE → apply per-line adjustments (when present); the confirm ceremony
    (``confirm_and_register``) runs the Draft→Confirmed transition with its TTL
    guard, classification, conversion and Sheets registration.

    REJECT → ``cancel_order`` with the owner as actor: Draft/Confirmed release
    every ACTIVE reservation; Picking/Ready for delivery restore the deducted
    stock with the audit trail (spec: cancellations release or restore stock).

    UNKNOWN → ``UnknownDecisionError`` — the owner is asked to repeat.
    """
    if decision.action is DecisionAction.REJECT:
        return cancel_order(session, order, actor="owner", now=now)
    if decision.action is DecisionAction.APPROVE:
        if decision.adjustments:
            _apply_line_adjustments(session, order, decision, quote)
        return order
    raise UnknownDecisionError("unresolved owner decision")


def _selection_state_updates(result, order, state: ConversationState) -> ConversationState:
    """Move a conversation from awaiting-confirm to the Case B selection turn."""
    needs = tuple(
        SourcingNeedItem(sku=m.sku, missing_quantity=m.missing_quantity) for m in result.missing
    )
    candidates = tuple(c for m in result.missing for c in m.candidates)
    return state.with_updates(
        awaiting_decision=False,
        order_id=order.order_id,
        items=(),
        parsed_order=None,
        sourcing_selection_pending=True,
        sourcing_needs=needs,
        sourcing_candidates=candidates,
    )


def build_dispatch_handler(
    session_factory: Callable[[], Session],
    sheets: SheetsWriter,
    *,
    searcher: SupplierCatalogSearcher | None = None,
) -> Callable[[InboundMessage, ConversationState | None, RoutingDecision], AgentOutcome]:
    """Build the wired DISPATCH agent: decision → order → ceremony (or cancel).

    Confirm/cancel replies on an ``awaiting_decision`` conversation run the
    real flow: ``parse_decision`` → load the order (``#N`` override or the
    state's ``order_id``) → ``apply_decision`` + ``confirm_and_register``.
    A stale quote rolls back with the re-quote error; a Sheets quarantine is
    tolerated (the order stays CONFIRMED and the error surfaces in chat).
    """

    def handler(
        message: InboundMessage,
        state: ConversationState | None,
        _decision: RoutingDecision,
    ) -> AgentOutcome:
        if state is None or not state.awaiting_decision:
            return AgentOutcome(state=state, reply="¿Sobre qué pedido querés decidir?")
        parsed = parse_decision(message.text or "")
        if parsed.action is DecisionAction.UNKNOWN:
            log_session_event(
                "dispatch",
                "decision_unknown",
                {"text": message.text},
                level="INFO",
            )
            return AgentOutcome(
                state=state,
                reply="Respondé 'aprobá' o 'rechazá' (podés agregar descuentos: '5% extra en clavos').",
            )
        ref = parse_order_reference(message.text or "")
        order_id = ref if ref is not None else state.order_id
        if order_id is None:
            log_session_event(
                "dispatch",
                "order_reference_missing",
                {"text": message.text},
                level="WARNING",
            )
            return AgentOutcome(
                state=state,
                reply="No sé qué pedido es: respondé con el número, ej. 'aprobá el pedido #3'.",
            )
        log_session_event(
            "dispatch",
            "decision_parsed",
            {
                "order_id": order_id,
                "action": parsed.action.value,
                "adjustments_count": len(parsed.adjustments),
            },
        )
        with session_factory() as session:
            order = session.get(Order, order_id)
            if order is None:
                log_session_event(
                    "dispatch",
                    "order_not_found",
                    {"order_id": order_id},
                    level="WARNING",
                )
                return AgentOutcome(state=state, reply=f"No encuentro el pedido #{order_id}.")
            try:
                if parsed.action is DecisionAction.APPROVE:
                    apply_decision(session, order, parsed)
                    result = confirm_and_register(
                        session,
                        order,
                        sheets=sheets,
                        searcher=searcher,
                        actor="owner",
                    )
                    reply = result.confirmation_text
                    log_session_event(
                        "dispatch",
                        "decision_approved",
                        {
                            "order_id": order.order_id,
                            "cancelled_case": result.cancelled_case,
                            "missing_count": len(result.missing),
                            "sheets_status": result.sheets_status.value,
                        },
                    )
                else:
                    apply_decision(session, order, parsed)
                    reply = (
                        f"Pedido #{order.order_id} cancelado; "
                        "las reservas fueron liberadas y el stock restaurado."
                    )
                    log_session_event(
                        "dispatch",
                        "decision_rejected",
                        {
                            "order_id": order.order_id,
                            "actor": "owner",
                        },
                    )
                session.commit()
            except RequiresRequoteError:
                session.rollback()
                log_session_event(
                    "dispatch",
                    "decision_requote_required",
                    {"order_id": order_id},
                    level="WARNING",
                )
                reply = (
                    f"El pedido #{order_id} tiene reservas vencidas: recotizalo antes de confirmar."
                )
            except (UnknownAdjustmentTargetError, InvalidTransitionError) as exc:
                session.rollback()
                log_session_event(
                    "dispatch",
                    "decision_failed",
                    {"order_id": order_id, "error": str(exc)},
                    level="ERROR",
                )
                reply = f"No pude aplicar la decisión: {exc}"
            except PendingConversionError:
                session.rollback()
                log_session_event(
                    "dispatch",
                    "decision_pending_conversion",
                    {"order_id": order_id},
                    level="WARNING",
                )
                reply = (
                    f"El pedido #{order_id} tiene precios pendientes de conversión; "
                    "cargá el tipo de cambio en Customer Orders y volvé a confirmar."
                )
            else:
                if parsed.action is DecisionAction.APPROVE and result.missing:
                    # Case B discovered at confirm: the selection turn takes over.
                    state = _selection_state_updates(result, order, state)
                else:
                    # Success: the decision conversation is closed.
                    state = state.with_updates(
                        awaiting_decision=False, order_id=None, items=(), parsed_order=None
                    )
        return AgentOutcome(state=state, reply=reply)

    return handler
