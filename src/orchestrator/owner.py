"""Owner sender gate: the pipeline's first edge check.

The owner is the only chat actor of the system: WhatsApp/Telegram inbound
messages are gated here, before any routing, against the configured owner
allowlist (``owner_telegram_chat_id`` for Telegram chat ids,
``owner_whatsapp_phone`` for WhatsApp numbers). Any other sender receives a
polite rejection and is never routed — no order is created, quoted or approved
for them.

When no owner key is configured for a channel the gate stays OPEN: that is the
documented rollback path that keeps the legacy customer intake working.
"""

from __future__ import annotations

from src.agents.customer import normalize_phone
from src.config import Settings


def _owner_chat_id(settings: Settings) -> str:
    """Configured Telegram chat id, trimmed (empty when not set)."""
    return (settings.owner_telegram_chat_id or "").strip()


def _owner_whatsapp(settings: Settings) -> str:
    """Configured WhatsApp owner phone, trimmed (empty when not set)."""
    return (settings.owner_whatsapp_phone or "").strip()


def is_owner_sender(sender_id: str, channel: str, settings: Settings) -> bool:
    """Return True when ``sender_id`` is the configured owner on ``channel``.

    Telegram senders are chat ids: compared as strings after trimming.
    WhatsApp senders are phone numbers: both sides are normalized to canonical
    E.164 before comparing, so any formatting of the same number matches.
    When no owner key is configured for the channel the gate is open (legacy
    intake); an unknown channel is never treated as the owner.
    """
    if channel == "telegram":
        configured = _owner_chat_id(settings)
        if not configured:
            return True
        return sender_id.strip() == configured
    if channel == "whatsapp":
        configured = _owner_whatsapp(settings)
        if not configured:
            return True
        owner_norm = normalize_phone(configured)
        sender_norm = normalize_phone(sender_id)
        if owner_norm is not None and sender_norm is not None:
            return owner_norm == sender_norm
        return sender_id.strip() == configured
    return False


def rejection_reply() -> str:
    """Polite reply for a sender that is not the owner."""
    return (
        "Hola: este chat es privado del dueño de la ferretería. "
        "Si querés hacer un pedido, escribile directamente por teléfono."
    )
