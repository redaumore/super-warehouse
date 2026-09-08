"""Pure text/phone normalization helpers shared across module boundaries.

Every function here was moved verbatim from its origin module so the
normalization seam is owned once, in the foundation layer, instead of being
reached across same-layer module edges:

- ``normalize_phone`` ← ``src.agents.customer`` (WhatsApp E.164 canonicalization);
- ``normalize_text`` ← ``src.agents.disambiguation`` (accent/punctuation folding).

This module is pure: stdlib and ``phonenumbers`` only — no db, no settings.
"""

from __future__ import annotations

import re
import unicodedata

import phonenumbers
from phonenumbers import PhoneNumber, PhoneNumberFormat
from phonenumbers.phonenumberutil import NumberParseException

_DEFAULT_REGION = "AR"

_WORD_RE = re.compile(r"[^\w\s]+")


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


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _WORD_RE.sub(" ", text).lower()
    return " ".join(text.split())
