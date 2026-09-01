"""Pure supplier validation and code-generation helpers.

Focused, dependency-light functions shared by the backoffice CRUD module
(``src/backoffice/suppliers.py``) and the supplier master-data flows:

- ``validate_cuit`` — Argentine CUIT mod-11 verifier (weights 5,4,3,2,7,6,5,4,3,2).
- ``normalize_e164_phone`` — strict E.164 via ``phonenumbers`` (does NOT insert
  the WhatsApp ``9``; distinct from ``src.agents.customer.normalize_phone``).
- ``normalize_whatsapp`` — the WhatsApp form (``+54 9``), consistent with the
  customer channel.
- ``validate_email`` — RFC 5322 via ``email_validator`` (no deliverability
  check — offline-safe).
- ``suggest_code`` / ``resolve_code`` — deterministic 3-char code generation
  from ``business_name`` with a collision variant loop (no DB round-trips, no
  random suffixes; the unique index is the backstop).
"""

from __future__ import annotations

import re
import unicodedata

from email_validator import EmailNotValidError
from email_validator import validate_email as _validate_email
from phonenumbers import (
    NumberParseException,
    PhoneNumberFormat,
    format_number,
    is_valid_number,
    parse,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.customer import normalize_phone
from src.db.models import Supplier

_CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


class CodeCollisionError(Exception):
    """No free 3-char code variant exists — the owner must type one manually."""


def validate_cuit(cuit: str) -> bool:
    """Validate an Argentine CUIT (11 digits) with the mod-11 check digit.

    Accepts plain digits or dashed input (``30-12345678-1``); returns ``False``
    for anything that is not a well-formed, verifier-passing CUIT.
    """
    cleaned = cuit.strip().replace("-", "")
    if len(cleaned) != 11 or not cleaned.isdigit():
        return False
    total = sum(int(digit) * weight for digit, weight in zip(cleaned[:10], _CUIT_WEIGHTS))
    check = (11 - (total % 11)) % 11
    if check == 10:  # remainder 1 → the verifier would be "10", never a valid CUIT
        return False
    return check == int(cleaned[10])


def normalize_e164_phone(raw: str, *, region: str = "AR") -> str | None:
    """Normalize a phone to strict E.164; ``None`` when unparseable/invalid.

    Unlike ``normalize_phone`` this does NOT insert the WhatsApp ``9`` for
    Argentine mobiles — ``phone`` is the plain E.164 line while ``whatsapp``
    keeps the WhatsApp form.
    """
    try:
        number = parse(raw, region)
    except NumberParseException:
        return None
    if not is_valid_number(number):
        return None
    return format_number(number, PhoneNumberFormat.E164)


def normalize_whatsapp(raw: str) -> str | None:
    """Normalize a phone to the WhatsApp form (``+54 9``), reusing the customer channel."""
    return normalize_phone(raw)


def validate_email(email: str) -> bool:
    """Validate an email per RFC 5322 (``email_validator``); deliverability off."""
    try:
        _validate_email(email.strip(), check_deliverability=False)
    except EmailNotValidError:
        return False
    return True


def _fold_token(token: str) -> str:
    """Strip accents and uppercase a token (NFKD + combining-mark removal)."""
    token = unicodedata.normalize("NFKD", token)
    token = "".join(ch for ch in token if not unicodedata.combining(ch))
    return token.upper()


def suggest_code(business_name: str) -> str:
    """Suggest a 3-char uppercase code from a business name.

    First letter of the first three tokens; when fewer than three tokens exist,
    pads from the first token's remaining letters. Only alphanumeric letters
    count. Empty input yields ``""``.
    """
    tokens = [token for token in re.split(r"\s+", business_name.strip()) if token]
    if not tokens:
        return ""
    letters: list[str] = []
    for token in tokens[:3]:
        folded = _fold_token(token)
        if folded and folded[0].isalnum():
            letters.append(folded[0])
    code = "".join(letters)
    if len(code) < 3:
        for ch in _fold_token(tokens[0])[1:]:
            if ch.isalnum():
                code += ch
                if len(code) == 3:
                    break
    return code[:3]


def normalize_code(raw: str) -> str:
    """Normalize a raw code to uppercase alphanumeric, first 3 chars."""
    folded = _fold_token(raw)
    return "".join(ch for ch in folded if ch.isalnum())[:3]


def _code_free(session: Session, code: str, *, exclude_id: int | None = None) -> bool:
    stmt = select(Supplier.id).where(Supplier.code == code)
    if exclude_id is not None:
        stmt = stmt.where(Supplier.id != exclude_id)
    return session.scalar(stmt) is None


def resolve_code(session: Session, raw: str, *, exclude_id: int | None = None) -> str:
    """Resolve a requested code to a free 3-char variant, rotating over A-Z0-9.

    Tries the code itself, then rotations of the third character (fixed
    two-char prefix), then two-character rotations (fixed first char). Raises
    ``CodeCollisionError`` when every variant is taken — the owner types the
    code manually. The DB unique index is the backstop for concurrent races.

    ``exclude_id`` lets an edit keep the supplier's own current code (the UI
    always resubmits the full form, so the row itself must not count as a
    collision).
    """
    code = normalize_code(raw)
    if not code:
        raise CodeCollisionError("cannot derive a code from an empty value")
    if _code_free(session, code, exclude_id=exclude_id):
        return code
    prefix = code[:2]
    for char in _CODE_ALPHABET:
        candidate = prefix + char
        if candidate != code and _code_free(session, candidate, exclude_id=exclude_id):
            return candidate
    for second in _CODE_ALPHABET:
        for third in _CODE_ALPHABET:
            candidate = code[0] + second + third
            if candidate != code and _code_free(session, candidate, exclude_id=exclude_id):
                return candidate
    raise CodeCollisionError(f"no free 3-char code variant for {code}")
