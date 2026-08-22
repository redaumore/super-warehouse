"""Channel adapters: Telegram (demo) and WhatsApp (production primary)."""

from src.channels.base import Channel, InboundMessage
from src.channels.whatsapp import WhatsAppChannel

__all__ = ["Channel", "InboundMessage", "WhatsAppChannel"]