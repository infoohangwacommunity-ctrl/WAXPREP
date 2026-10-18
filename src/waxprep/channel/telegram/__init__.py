"""Telegram Bot API channel adapter."""

from waxprep.channel.telegram.client import TelegramClient
from waxprep.channel.telegram.normalize import normalize_telegram_update
from waxprep.channel.telegram.outbox import TelegramOutbox, TelegramOutboxConfig
from waxprep.channel.telegram.routes import build_telegram_router

__all__ = [
    "TelegramClient",
    "TelegramOutbox",
    "TelegramOutboxConfig",
    "normalize_telegram_update",
    "build_telegram_router",
]
