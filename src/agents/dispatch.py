"""Dispatch & owner assistant agent: notification and approve/reject (task 2.7).

Owns the owner-facing conversation tools of the pipeline:

- ``notify_owner`` — sends the quote to the owner through a mockable notifier
  interface (Telegram today, WhatsApp Cloud API in Phase 3);
- ``parse_decision`` — interprets the owner's reply ("sí, aprobá" vs
  "no, rechazá", optionally with per-line adjustments like "hacé un 5% de
  descuento extra en clavos");
- ``apply_decision`` — applies the decision to the order through the lifecycle:
  approval (with adjustments re-pricing the affected lines) or rejection
  (releasing every reservation immediately).

Decision parsing is a pure function; the notifier is a protocol so unit tests
never touch the network.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.disambiguation import normalize_text
from src.agents.sales import Quote
from src.db.models import Order, OrderItem
from src.order_lifecycle.state import approve_order, reject_order

_CENT = Decimal("0.01")

_APPROVE_RE = re.compile(r"\b(aprob|dale|s[ií]|ok|confirm|acept|adelante)", re.IGNORECASE)
_REJECT_RE = re.compile(r"\b(rechaz|nop|negativ|no(?!\w))", re.IGNORECASE)
_ADJUST_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%\s*(?:extra\s+)?(?:de\s+)?descuento\s+(?:extra\s+)?(?:en\s+)?([^.,;]+)",
    re.IGNORECASE,
)


class UnknownDecisionError(Exception):
    """The owner's reply could not be resolved to approve or reject."""


class UnknownAdjustmentTargetError(Exception):
    """An adjustment names a line that does not exist in the order/quote."""


class Notifier(Protocol):
    """Mockable outbound boundary (Telegram today, WhatsApp in Phase 3)."""

    def send_text(self, recipient: str, text: str) -> None:
        """Send a text message to ``recipient``."""


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


def notify_owner(
    notifier: Notifier,
    owner_phone: str,
    quote: Quote,
    order_id: int,
    customer_name: str | None = None,
) -> None:
    """Send the quote to the owner and leave the order awaiting the decision."""
    notifier.send_text(owner_phone, format_quote_message(quote, order_id, customer_name))


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
    items = {item.sku: item for item in session.scalars(
        select(OrderItem).where(OrderItem.order_id == order.order_id)
    )}
    for adjustment in decision.adjustments:
        sku = _resolve_adjustment_sku(quote, adjustment.sku)
        item = items.get(sku)
        if item is None:
            raise UnknownAdjustmentTargetError(f"sku not in order: {sku}")
        pct = Decimal(adjustment.extra_discount_pct)
        new_final = (item.final_price * (Decimal(1) - pct)).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
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