"""Owner sender gate tests (task 1.4).

The gate is the pipeline's first check: the owner is the only chat actor, and
any other sender must be rejected before routing. The matrix covers both
channels — Telegram chat ids compared as strings, WhatsApp numbers compared in
canonical E.164 — plus the legacy fallback (no owner key configured → gate
open, per the design's rollback path).
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.orchestrator.owner import is_owner_sender, rejection_reply

OWNER_CHAT_ID = "123456789"
OWNER_PHONE = "+54 9 11 5555-0000"
OWNER_PHONE_CANONICAL = "+5491155550000"


def _settings(**overrides) -> Settings:
    base = {
        "owner_telegram_chat_id": OWNER_CHAT_ID,
        "owner_whatsapp_phone": OWNER_PHONE,
    }
    base.update(overrides)
    return Settings(**base)


# ------------------------------------------------------- Telegram (chat ids)


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        (OWNER_CHAT_ID, True),
        (" 123456789 ", True),  # trimming tolerates channel formatting
        ("987654321", False),
        ("", False),
    ],
)
def test_telegram_gate_matches_only_configured_chat_id(sender, expected):
    """Solo el chat id configurado pasa el gate de Telegram."""
    assert is_owner_sender(sender, "telegram", _settings()) is expected


# ------------------------------------------------------- WhatsApp (phones)


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        (OWNER_PHONE, True),
        (OWNER_PHONE_CANONICAL, True),  # canonical E.164 form
        ("5491155550000", True),  # national form normalizes to the same number
        ("11 5555-0000", True),  # bare national digits
        ("+5491133334444", False),
        ("no-es-un-telefono", False),
    ],
)
def test_whatsapp_gate_normalizes_phone_before_compare(sender, expected):
    """Los números de WhatsApp se normalizan a E.164 antes de comparar."""
    assert is_owner_sender(sender, "whatsapp", _settings()) is expected


# ------------------------------------------------- legacy fallback (gate open)


def test_no_owner_keys_keeps_legacy_gate_open():
    """Sin claves de dueño configuradas el gate queda abierto (intake legacy)."""
    settings = Settings(owner_telegram_chat_id="", owner_whatsapp_phone="")
    assert is_owner_sender("cualquier-sender", "telegram", settings) is True
    assert is_owner_sender("+5491199990000", "whatsapp", settings) is True


def test_partial_keys_gate_only_the_configured_channel():
    """Una sola clave configurada gatea solo ese canal; el otro queda abierto."""
    settings = Settings(owner_telegram_chat_id=OWNER_CHAT_ID, owner_whatsapp_phone="")
    assert is_owner_sender(OWNER_CHAT_ID, "telegram", settings) is True
    assert is_owner_sender("otro-chat", "telegram", settings) is False
    assert is_owner_sender("+5491199990000", "whatsapp", settings) is True


def test_unknown_channel_never_passes_the_gate():
    """Un canal desconocido nunca se trata como el dueño."""
    settings = Settings(owner_telegram_chat_id=OWNER_CHAT_ID, owner_whatsapp_phone=OWNER_PHONE)
    assert is_owner_sender(OWNER_CHAT_ID, "sms", settings) is False


def test_rejection_reply_is_polite_and_does_not_route():
    """El rechazo es una respuesta cortés, nunca un enrutamiento."""
    reply = rejection_reply()
    assert reply  # non-empty
    assert "Hola" in reply
    assert "dueño" in reply
