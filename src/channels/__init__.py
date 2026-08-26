"""Channel adapters: Telegram (demo) and WhatsApp (production primary).

``CHANNELS`` is the app-wide registry keyed by channel name, shared by the
intake webhook (routing inbound payloads to the right adapter) and the pipeline
(sending replies back on the sender's channel).
"""

from src.channels.base import Channel, InboundMessage
from src.channels.telegram import TelegramChannel
from src.channels.whatsapp import WhatsAppChannel

CHANNELS: dict[str, Channel] = {
    "telegram": TelegramChannel(),
    "whatsapp": WhatsAppChannel(),
}

__all__ = ["CHANNELS", "Channel", "InboundMessage", "TelegramChannel", "WhatsAppChannel"]
