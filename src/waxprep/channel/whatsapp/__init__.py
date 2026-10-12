"""WhatsApp Cloud API channel adapter."""

from waxprep.channel.whatsapp.client import WhatsAppClient
from waxprep.channel.whatsapp.handler import (
    WhatsAppInboundHandler,
    WhatsAppInboundConfig,
    InboundResult,
)
from waxprep.channel.whatsapp.normalize import normalize_webhook_payload
from waxprep.channel.whatsapp.outbox import WhatsAppOutbox, OutboxConfig
from waxprep.channel.whatsapp.security import verify_signature, verify_webhook_challenge

__all__ = [
    "WhatsAppClient",
    "WhatsAppInboundHandler",
    "WhatsAppInboundConfig",
    "InboundResult",
    "WhatsAppOutbox",
    "OutboxConfig",
    "normalize_webhook_payload",
    "verify_signature",
    "verify_webhook_challenge",
]
