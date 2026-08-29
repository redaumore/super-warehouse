"""Dispatch agent: approve/reject parsing and the wired approval flow.

Owns the owner-facing decision tools of the pipeline:

- ``parse_decision`` — interprets the owner's reply ("sí, aprobá" vs
  "no, rechazá", optionally with per-line adjustments like "hacé un 5% de
  descuento extra en clavos");
- ``parse_order_reference`` — extracts an explicit ``pedido #N`` reference so
  a decision can target a specific order instead of the rehydrated latest one;
- ``apply_decision`` — applies the decision to the order through the lifecycle:
  approval (with adjustments re-pricing the affected lines) or rejection
  (releasing every reservation immediately);
- ``build_dispatch_handler`` — the wired DISPATCH agent turn: parse decision →
  load order (``#N`` override or the conversation's ``order_id``) →
  ``apply_decision`` + ``register_approved_order`` (Sheets). A Sheets
  quarantine rolls the approval back: the order stays PENDING and the owner
  gets an error reply in chat.

Decision parsing is pure; the handler is built with injectable boundaries
(session factory + ``SheetsWriter``) so unit tests never touch the network.
The old ``owner_phone`` push (``notify_owner``) is gone — approvals,
rejections and errors are in-chat replies.
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
from src.orchestrator.approval import SheetsRegistrationError, register_approved_order
from src.orchestrator.router import AgentOutcome, RoutingDecision
from src.orchestrator.session import ConversationState
from src.order_lifecycle.state import (
    InvalidTransitionError,
    RequiresRequoteError,
    approve_order,
    reject_order,
)

_CENT = Decimal("0.01")

_APPROVE_RE = re.compile(r"\b(aprob|dale|s[ií]|ok|confirm|acept|adelante)", re.IGNORECASE)
_REJECT_RE = re.compile(r"\b(rechaz|nop|negativ|no(?!\w))", re.IGNORECASE)
_ADJUST_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%\s*(?:extra\s+)?(?:de\s+)?descuento\s+(?:extra\s+)?(?:en\s+)?([^.,;]+)",
    re.IGNORECASE,
)
_ORDER_REF_RE = re.compile(r"#\s*(\d+)")


class UnknownDecisionError(Exception):
    """The owner's reply could not be resolved to approve or reject."""


class UnknownAdjustmentTargetError(Exception):
    """An adjustment names a line that does not exist in the order/quote."""


@dataclass(frozen=True)
class LineAdjustment:
    """An owner adjustment: extra discount percentage on one line."""

    sku: str
    extra_discount_pct: Decimal


class DecisionAction(str, enum.Enum):
    """Owner decision outcome: approve, reject, or unresolved."""

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
    """Resolve an owner reply to approve / reject (+ per-line adjustments)."""
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

    APPROVE → apply per-line adjustments (when present) and move the order to
    APPROVED. Raises ``RequiresRequoteError`` (from the lifecycle) when the
    order's reservations have expired: the caller must re-quote first.

    REJECT → release every reservation immediately and move the order to
    REJECTED (spec: reserved stock becomes available to other customers).

    UNKNOWN → ``UnknownDecisionError`` — the owner is asked to repeat.
    """
    if decision.action is DecisionAction.REJECT:
        return reject_order(session, order, now=now)
    if decision.action is DecisionAction.APPROVE:
        if decision.adjustments:
            _apply_line_adjustments(session, order, decision, quote)
        return approve_order(session, order, now=now)
    raise UnknownDecisionError("unresolved owner decision")


def build_dispatch_handler(
    session_factory: Callable[[], Session],
    sheets: SheetsWriter,
) -> Callable[[InboundMessage, ConversationState | None, RoutingDecision], AgentOutcome]:
    """Build the wired DISPATCH agent: decision → order → lifecycle + Sheets.

    Approve/reject replies on an ``awaiting_decision`` conversation run the
    real approval flow: ``parse_decision`` → load the order (``#N`` override
    or the state's ``order_id``) → ``apply_decision`` + ``register_approved_order``.
    A Sheets quarantine rolls the whole approval back (order stays PENDING)
    and the owner gets an error reply in chat.
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
            return AgentOutcome(
                state=state,
                reply="Respondé 'aprobá' o 'rechazá' (podés agregar descuentos: '5% extra en clavos').",
            )
        ref = parse_order_reference(message.text or "")
        order_id = ref if ref is not None else state.order_id
        if order_id is None:
            return AgentOutcome(
                state=state,
                reply="No sé qué pedido es: respondé con el número, ej. 'aprobá el pedido #3'.",
            )
        with session_factory() as session:
            order = session.get(Order, order_id)
            if order is None:
                return AgentOutcome(state=state, reply=f"No encuentro el pedido #{order_id}.")
            try:
                if parsed.action is DecisionAction.APPROVE:
                    apply_decision(session, order, parsed)
                    result = register_approved_order(session, order, sheets=sheets)
                    reply = result.confirmation_text
                else:
                    apply_decision(session, order, parsed)
                    reply = f"Pedido #{order.order_id} rechazado; las reservas fueron liberadas."
                session.commit()
            except RequiresRequoteError:
                session.rollback()
                reply = (
                    f"El pedido #{order_id} tiene reservas vencidas: recotizalo antes de aprobar."
                )
            except (UnknownAdjustmentTargetError, InvalidTransitionError) as exc:
                session.rollback()
                reply = f"No pude aplicar la decisión: {exc}"
            except SheetsRegistrationError:
                session.rollback()
                reply = (
                    f"No pude registrar el pedido #{order_id} en Google Sheets; "
                    "sigue pendiente. Revisá la configuración y volvé a aprobarlo."
                )
            else:
                # Success: the decision conversation is closed.
                state = state.with_updates(
                    awaiting_decision=False, order_id=None, items=(), parsed_order=None
                )
        return AgentOutcome(state=state, reply=reply)

    return handler
