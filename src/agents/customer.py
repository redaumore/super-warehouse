"""Customer agent: phone normalization and identity resolution.

Normalizes an inbound phone string to a canonical E.164 form and resolves it
against the `clientes` table. Argentine (country code 54) numbers are rendered
in the WhatsApp mobile form (`+54 9 …`) so formatting variants reconcile to the
same customer — the spec's "formatting differences reconciled" scenario.

Outcomes follow the clients-and-price-lists spec:
- parseable and registered → ``KNOWN`` (pricing uses this customer's condition);
- parseable but not registered → ``UNKNOWN`` (flagged for later onboarding);
- unparseable → ``INVALID`` (never silently guessed).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumber, PhoneNumberFormat
from phonenumbers.phonenumberutil import NumberParseException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Cliente

_DEFAULT_REGION = "AR"


class PhoneStatus(str, enum.Enum):
    """Outcome of resolving a raw phone string."""

    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    KNOWN = "KNOWN"


@dataclass(frozen=True)
class PhoneLookup:
    """Result of resolving a raw phone against the customer table."""

    status: PhoneStatus
    normalized: str | None = None
    customer: Cliente | None = None


def _to_whatsapp_e164(number: PhoneNumber) -> str:
    """Render an Argentine number in WhatsApp mobile form (+54 9 …).

    WhatsApp customers always reach the store from a mobile line, so a national
    number without the trunk prefix ``9`` (e.g. ``11 5555 1234``) is completed
    to ``+54 9 11 5555 1234``. This keeps every variant of the same number
    converging on one canonical form. Landline rendering is out of MVP scope.
    """
    e164 = phonenumbers.format_number(number, PhoneNumberFormat.E164)
    if number.country_code == 54 and not str(number.national_number).startswith("9"):
        return f"+549{number.national_number}"
    return e164


def normalize_phone(raw: str, *, region: str = _DEFAULT_REGION) -> str | None:
    """Normalize a phone string to canonical E.164; ``None`` when unparseable.

    ``region`` is the default region for numbers without an explicit country
    code (the store's home country).
    """
    try:
        number = phonenumbers.parse(raw, region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return _to_whatsapp_e164(number)


def lookup_phone(session: Session, raw: str, *, region: str = _DEFAULT_REGION) -> PhoneLookup:
    """Resolve a raw phone to a stored customer, flagging INVALID / UNKNOWN / KNOWN."""
    normalized = normalize_phone(raw, region=region)
    if normalized is None:
        return PhoneLookup(status=PhoneStatus.INVALID)
    customer = session.scalar(select(Cliente).where(Cliente.telefono_norm == normalized))
    if customer is None:
        return PhoneLookup(status=PhoneStatus.UNKNOWN, normalized=normalized)
    return PhoneLookup(status=PhoneStatus.KNOWN, normalized=normalized, customer=customer)
