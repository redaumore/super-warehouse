"""Structured order intake: parse free-form customer text into order fields.

After transcription, the intake step extracts the structured order fields the
whatsapp-order-intake spec requires: customer name (best effort), a list of
items with quantities, and an optional delivery date. The parser is a pure
function over text — no DB, no LLM — behind the ``OrderParser`` protocol so
tests can fake it and a smarter (LLM-backed) parser can replace it later.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol


@dataclass(frozen=True)
class ParsedItem:
    """One order line extracted from free text."""

    description: str
    quantity: int = 1


@dataclass(frozen=True)
class ParsedOrder:
    """Structured order fields extracted from a transcribed/text message."""

    customer_name: str | None = None
    items: tuple[ParsedItem, ...] = ()
    delivery_date: date | None = None


class OrderParser(Protocol):
    """Parse a message into structured order fields (or None when not an order)."""

    def parse(self, text: str, *, now: date | None = None) -> ParsedOrder | None:
        """Return the parsed order, or ``None`` when the text is not an order.

        ``now`` is the reference date for the delivery-date resolver (tests
        inject it; production defaults to today).
        """


# --- fuzzy delivery-date resolution -----------------------------------------

_WEEKDAY_NAMES = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "miércoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}

_ORDER_INTENT = (
    "quiero",
    "necesito",
    "me falta",
    "faltan",
    "pedido",
    "comprar",
    "tenés",
    "tienen",
    "stock",
    "disponible",
    "precio",
    "cuánto",
    "cuanto",
)

_ITEM_RE = re.compile(
    r"(?P<qty>\d+)\s*(?:x|unidades?|un|u|kg|kilos)?\s*(?:de\s+)?"
    r"(?P<desc>[a-záéíóúñü](?:(?!\s+para\b)[\w áéíóúñü\-]){1,60})",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"(?:soy|para|de parte de|cliente)\s+"
    r"([a-záéíóúñü]+(?:\s+(?!y\b|quiero\b|necesito\b|necesitamos\b|comprar\b)[a-záéíóúñü]+){0,3})",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(r"\bel\s+(\d{1,2})(?:[/-](\d{1,2}))?")


def _fold(text: str) -> str:
    """Lowercase and strip accents for phrase matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def resolve_delivery_date(text: str, *, now: date | None = None) -> date | None:
    """Resolve a Spanish delivery-date phrase to a concrete date.

    Handles "hoy", "mañana", "pasado mañana", weekday names ("el viernes"),
    "la semana que viene", "el finde / fin de semana" and "para el 5" (day of
    the current or next month). Returns ``None`` when no phrase is found; the
    date is informational only and never drives scheduling.
    """
    reference = now or datetime.now(UTC).date()
    folded = _fold(text)

    if "pasado mañana" in folded or "pasado manana" in folded:
        return reference + timedelta(days=2)
    if "mañana" in folded or "manana" in folded:
        return reference + timedelta(days=1)
    if "hoy" in folded:
        return reference
    if "la semana que viene" in folded:
        return reference + timedelta(days=7)
    if "fin de semana" in folded or "el finde" in folded or "finde" in folded:
        days_until_saturday = (5 - reference.weekday()) % 7
        return reference + timedelta(days=days_until_saturday or 7)
    for name, weekday in _WEEKDAY_NAMES.items():
        if name in folded:
            days = (weekday - reference.weekday()) % 7
            return reference + timedelta(days=days or 7)

    match = _DAY_MONTH_RE.search(folded)
    if match:
        day = int(match.group(1))
        month = int(match.group(2)) if match.group(2) else None
        try:
            if month is not None:
                candidate = date(reference.year, month, day)
                if candidate < reference:
                    candidate = date(reference.year + 1, month, day)
                return candidate
            candidate = date(reference.year, reference.month, day)
            if candidate < reference:
                year, next_month = reference.year, reference.month + 1
                if next_month > 12:
                    next_month, year = 1, year + 1
                candidate = date(year, next_month, day)
            return candidate
        except ValueError:
            return None
    return None


def _extract_items(text: str) -> tuple[ParsedItem, ...]:
    """Pull quantity + description pairs, splitting on 'y', commas and semicolons."""
    items: list[ParsedItem] = []
    for chunk in re.split(r"\s+y\s+|,|;", text):
        match = _ITEM_RE.search(chunk)
        if match is None:
            continue
        description = match.group("desc").strip(" .")
        if len(description) < 2:
            continue
        items.append(ParsedItem(description=description, quantity=int(match.group("qty"))))
    return tuple(items)


def _extract_customer_name(text: str) -> str | None:
    """Best-effort customer name from 'soy/para/de parte de <name>'."""
    match = _NAME_RE.search(text)
    if match is None:
        return None
    name = " ".join(match.group(1).split())
    return name[:1].upper() + name[1:] if name else None


class SimpleOrderParser:
    """Rule-based parser: quantities + descriptions, fuzzy dates, best-effort name.

    Returns ``None`` when the text shows no order intent (a plain chat message
    keeps the legacy conversational routing); a message with order intent but
    no resolvable items parses to an empty ``ParsedOrder`` so the flow asks the
    customer to specify items.
    """

    def parse(self, text: str, *, now: date | None = None) -> ParsedOrder | None:
        if not text or not text.strip():
            return None
        items = _extract_items(text)
        folded = _fold(text)
        has_intent = any(word in folded for word in _ORDER_INTENT)
        if not items and not has_intent:
            return None
        return ParsedOrder(
            customer_name=_extract_customer_name(text),
            items=items,
            delivery_date=resolve_delivery_date(text, now=now),
        )
