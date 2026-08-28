"""Customer-by-name resolution for the owner pivot.

The owner names the customer in the order message; the customer is resolved
against ``Cliente.nombre_comercial``, never by the sender's phone (the owner
sender is a single actor). Resolution order per the spec: exact match first,
then accent/case-folded containment — one match auto-selects, two or more ask
the owner to disambiguate, zero offers in-chat creation.

The matching itself is a PURE function over ``(customer_id, name)`` rows so it
is unit-testable without a database; ``resolve_customer_name`` is the DB-backed
resolver that loads the real ``Cliente`` rows. The in-chat creation command
(``nuevo cliente <nombre> <teléfono>``) and the numbered disambiguation pick
are also parsed here.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Cliente


class CustomerResolutionKind(str, enum.Enum):
    """Outcome of resolving a customer name."""

    EXACT = "EXACT"
    FOLDED = "FOLDED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class CustomerNameMatch:
    """Pure matching outcome over ``(customer_id, name)`` rows — no ORM."""

    kind: CustomerResolutionKind
    ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CustomerResolution:
    """Resolved customer outcome carrying the real ``Cliente`` rows."""

    kind: CustomerResolutionKind
    candidate: Cliente | None = None
    candidates: tuple[Cliente, ...] = ()


_WORD_RE = re.compile(r"[^\w\s]+")
_NUEVO_CLIENTE_RE = re.compile(r"^\s*nuevo\s+cliente\s+(.+)$", re.IGNORECASE)


def _fold(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _WORD_RE.sub(" ", text).casefold()
    return " ".join(text.split())


def match_by_name(name: str, rows: Sequence[tuple[int, str]]) -> CustomerNameMatch:
    """Match ``name`` against ``(customer_id, nombre_comercial)`` rows (pure).

    Exact (folded) match first: exactly one hit auto-selects, two or more are
    ambiguous. Then folded containment: one hit resolves as FOLDED, two or
    more as AMBIGUOUS. Nothing above the floor is NOT_FOUND.
    """
    folded = _fold(name)
    exact = [customer_id for customer_id, row_name in rows if _fold(row_name) == folded]
    if len(exact) == 1:
        return CustomerNameMatch(kind=CustomerResolutionKind.EXACT, ids=(exact[0],))
    if len(exact) > 1:
        return CustomerNameMatch(kind=CustomerResolutionKind.AMBIGUOUS, ids=tuple(exact))
    if not folded:
        return CustomerNameMatch(kind=CustomerResolutionKind.NOT_FOUND)
    contained = [customer_id for customer_id, row_name in rows if folded in _fold(row_name)]
    if len(contained) == 1:
        return CustomerNameMatch(kind=CustomerResolutionKind.FOLDED, ids=(contained[0],))
    if len(contained) > 1:
        return CustomerNameMatch(kind=CustomerResolutionKind.AMBIGUOUS, ids=tuple(contained))
    return CustomerNameMatch(kind=CustomerResolutionKind.NOT_FOUND)


def resolve_customer_name(session: Session, name: str) -> CustomerResolution:
    """Resolve ``name`` against the clientes table (DB-backed)."""
    rows = [
        (client.customer_id, client.nombre_comercial)
        for client in session.scalars(select(Cliente).order_by(Cliente.nombre_comercial))
    ]
    match = match_by_name(name, rows)
    if match.kind is CustomerResolutionKind.AMBIGUOUS:
        clients: list[Cliente] = []
        for customer_id in match.ids:
            client = session.get(Cliente, customer_id)
            if client is not None:
                clients.append(client)
        return CustomerResolution(kind=match.kind, candidates=tuple(clients))
    if match.kind is CustomerResolutionKind.NOT_FOUND:
        return CustomerResolution(kind=match.kind)
    client = session.get(Cliente, match.ids[0])
    if client is None:
        return CustomerResolution(kind=CustomerResolutionKind.NOT_FOUND)
    return CustomerResolution(kind=match.kind, candidate=client)


def parse_create_client_command(text: str) -> tuple[str, str] | None:
    """Parse ``nuevo cliente <nombre> <teléfono>`` into ``(nombre, tel)``.

    The name may contain spaces (e.g. ``Ferretería Don Juan``); the phone is
    the last token. Returns ``None`` when the text is not a create command or
    the command carries no name/phone pair.
    """
    match = _NUEVO_CLIENTE_RE.match(text or "")
    if match is None:
        return None
    rest = " ".join(match.group(1).split())
    nombre, telefono = rest.rsplit(" ", 1) if " " in rest else (rest, "")
    if not nombre or not telefono:
        return None
    return nombre, telefono


def parse_customer_pick(text: str, candidates: Sequence[Cliente]) -> Cliente | None:
    """Map a numbered disambiguation pick (1-based) to a candidate; None when invalid."""
    raw = (text or "").strip()
    if not raw.isdigit():
        return None
    number = int(raw)
    if not 1 <= number <= len(candidates):
        return None
    return candidates[number - 1]


def format_customer_menu(candidates: Sequence[Cliente]) -> str:
    """Render the numbered disambiguation menu for the owner."""
    lines = ["Hay varios clientes con ese nombre; elegí el número:"]
    lines.extend(
        f"{i}) {candidate.nombre_comercial}" for i, candidate in enumerate(candidates, start=1)
    )
    return "\n".join(lines)
