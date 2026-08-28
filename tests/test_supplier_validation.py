"""Pure unit tests for the supplier validation/codegen helpers.

No DB, no network: CUIT mod-11, strict E.164 vs WhatsApp phone forms, RFC 5322
email, and the deterministic 3-char code suggestion/collision loop. The code
collision tests use a stub session whose ``scalar`` answers whether a code is
already taken.
"""

from __future__ import annotations

import pytest

from src.supplier.validation import (
    CodeCollisionError,
    normalize_e164_phone,
    normalize_whatsapp,
    resolve_code,
    suggest_code,
    validate_cuit,
    validate_email,
)

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


class _FakeSession:
    """Stub session: ``scalar(select(Supplier.id).where(code == X))`` only."""

    def __init__(self, taken: set[str]) -> None:
        self._taken = set(taken)

    def scalar(self, stmt) -> int | None:
        compiled = stmt.compile()
        params = dict(compiled.params)
        value = params.get("code_1")
        if value is None and params:
            value = next(iter(params.values()))
        return 1 if value in self._taken else None


def _all_variants(base: str) -> set[str]:
    """Every code the resolver can try for ``base`` (base + rotations)."""
    codes = {base}
    codes |= {base[:2] + ch for ch in _ALPHABET}
    codes |= {base[0] + a + b for a in _ALPHABET for b in _ALPHABET}
    return codes


# ---------------------------------------------------------------- CUIT (mod-11)


@pytest.mark.parametrize(
    "cuit",
    [
        "20111111112",
        "20304050609",
        "30-12345678-1",
        " 20111111112 ",
    ],
)
def test_validate_cuit_accepts_valid(cuit: str):
    assert validate_cuit(cuit) is True


@pytest.mark.parametrize(
    "cuit",
    [
        "20111111110",  # wrong verifier digit
        "2011111111",  # 10 digits
        "201111111123",  # 12 digits
        "abcdefghijk",
        "2011111111X",
        "",
        "20111111111",  # verifier would be 2, given 1
    ],
)
def test_validate_cuit_rejects_invalid(cuit: str):
    assert validate_cuit(cuit) is False


# ------------------------------------------------------------ phone (E.164)


def test_e164_mobile_keeps_strict_e164_without_whatsapp_9():
    assert normalize_e164_phone("11 5555-1234") == "+541155551234"


def test_e164_keeps_explicit_whatsapp_9_when_given():
    assert normalize_e164_phone("+54 9 11 5555-1234") == "+5491155551234"


def test_e164_landline_normalizes_without_9():
    assert normalize_e164_phone("+54 351 455-1234") == "+543514551234"


@pytest.mark.parametrize("raw", ["no-es-telefono", "", "123"])
def test_e164_unparseable_returns_none(raw: str):
    assert normalize_e164_phone(raw) is None


def test_whatsapp_uses_whatsapp_form():
    assert normalize_whatsapp("11 5555-1234") == "+5491155551234"


def test_whatsapp_unparseable_returns_none():
    assert normalize_whatsapp("no-es-telefono") is None


# ------------------------------------------------------------------ email


@pytest.mark.parametrize("email", ["owner@example.com", "  Owner@Example.com "])
def test_validate_email_accepts_valid(email: str):
    assert validate_email(email) is True


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "user@.com", ""])
def test_validate_email_rejects_malformed(email: str):
    assert validate_email(email) is False


# ------------------------------------------------------------ suggest_code


def test_suggest_code_three_tokens_first_letters():
    assert suggest_code("Comercial Mayorista SA") == "CMS"


def test_suggest_code_two_tokens_pads_from_first():
    assert suggest_code("Ferremax SA") == "FSE"


def test_suggest_code_single_token_pads_from_first():
    assert suggest_code("Ferremax") == "FER"


def test_suggest_code_strips_accents():
    assert suggest_code("Álvarez Hnos") == "AHL"


def test_suggest_code_uppercases_and_trims():
    assert suggest_code("  ferretería del sur  ") == "FDS"


def test_suggest_code_empty_input():
    assert suggest_code("") == ""
    assert suggest_code("   ") == ""


# ------------------------------------------------------------ resolve_code


def test_resolve_code_returns_normalized_free_code():
    session = _FakeSession(set())
    assert resolve_code(session, "abc") == "ABC"


def test_resolve_code_rotates_third_char_on_collision():
    session = _FakeSession({"ABC", "ABA", "ABB"})
    assert resolve_code(session, "abc") == "ABD"


def test_resolve_code_two_char_rotation_when_third_char_exhausted():
    taken = {"ABC"} | {f"AB{ch}" for ch in _ALPHABET}
    session = _FakeSession(taken)
    assert resolve_code(session, "abc") == "AAA"


def test_resolve_code_exhausted_raises_collision_error():
    session = _FakeSession(_all_variants("PMS"))
    with pytest.raises(CodeCollisionError, match="no free 3-char code variant"):
        resolve_code(session, "pms")


def test_resolve_code_empty_raises_collision_error():
    session = _FakeSession(set())
    with pytest.raises(CodeCollisionError):
        resolve_code(session, "  ")