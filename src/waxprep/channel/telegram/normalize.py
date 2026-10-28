"""Parse Telegram Update JSON into NormalizedInbound messages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from waxprep.channel.contracts import (
    CONTENT_KIND_INTERACTIVE_BUTTON,
    CONTENT_KIND_TEXT,
    CONTENT_KIND_UNKNOWN,
    NormalizedInbound,
)


def _normalize_callback_query(
    payload: dict[str, Any],
) -> list[NormalizedInbound]:
    """Normalize a Telegram ``callback_query`` update (inline button click).

    The button's ``callback_data`` is the OPAQUE id WAXPREP issued when
    rendering the inline keyboard — never user-visible text and never an
    authorization fact. Authorization happens later against the stored
    one-shot InteractiveOffer for the clicking student.
    """
    cq = payload.get("callback_query")
    if not isinstance(cq, dict):
        return []
    data = cq.get("data")
    if data is None:
        return []
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    from_user = cq.get("from") or {}
    if from_user.get("is_bot"):
        return []
    chat_id = chat.get("id")
    if chat_id is None:
        return []
    cq_id = cq.get("id")
    if cq_id is None:
        return []
    message_id = msg.get("message_id")
    occurred_raw = cq.get("message", {}).get("date")
    if isinstance(occurred_raw, (int, float)):
        occurred_at = datetime.fromtimestamp(occurred_raw, tz=UTC)
    else:
        occurred_at = datetime.now(tz=UTC)
    return [
        NormalizedInbound(
            channel="telegram",
            provider_sender_id=str(chat_id),
            wax_id=None,
            # Unique per click: duplicate webhook retries of the SAME
            # callback arrive with the same callback_query id, so this is
            # the idempotency key (like provider_message_id for texts).
            provider_message_id=str(cq_id),
            occurred_at=occurred_at,
            text=None,
            content_kind=CONTENT_KIND_INTERACTIVE_BUTTON,
            reply_to_provider_id=(
                str(message_id) if message_id is not None else None
            ),
            structured={
                "button_id": str(data),
                "callback_query_id": str(cq_id),
                "telegram_user_id": from_user.get("id"),
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name"),
                "chat_type": chat.get("type"),
            },
        )
    ]


def normalize_telegram_update(payload: dict[str, Any]) -> list[NormalizedInbound]:
    """Extract inbound student messages from a Telegram Update object."""
    if isinstance(payload.get("callback_query"), dict):
        return _normalize_callback_query(payload)
    msg = payload.get("message") or payload.get("edited_message")
    if not isinstance(msg, dict):
        return []

    chat = msg.get("chat") or {}
    from_user = msg.get("from") or {}
    if from_user.get("is_bot"):
        return []

    chat_id = chat.get("id")
    if chat_id is None:
        return []
    sender = str(chat_id)

    message_id = msg.get("message_id")
    if message_id is None:
        return []
    provider_message_id = str(message_id)

    date_raw = msg.get("date")
    if isinstance(date_raw, (int, float)):
        occurred_at = datetime.fromtimestamp(date_raw, tz=UTC)
    else:
        occurred_at = datetime.now(tz=UTC)

    text = msg.get("text") or msg.get("caption")
    content_kind = CONTENT_KIND_TEXT
    if text is None:
        # Non-text (sticker, photo without caption, etc.)
        content_kind = CONTENT_KIND_UNKNOWN
        if msg.get("photo"):
            text = "[photo]"
        elif msg.get("document"):
            text = "[document]"
        elif msg.get("voice") or msg.get("audio"):
            text = "[audio]"
        elif msg.get("sticker"):
            text = "[sticker]"
        else:
            text = "[unsupported message]"

    reply = msg.get("reply_to_message") or {}
    reply_to = str(reply["message_id"]) if reply.get("message_id") is not None else None

    return [
        NormalizedInbound(
            channel="telegram",
            provider_sender_id=sender,
            wax_id=None,
            provider_message_id=provider_message_id,
            occurred_at=occurred_at,
            text=text,
            content_kind=content_kind,
            reply_to_provider_id=reply_to,
            structured={
                "telegram_user_id": from_user.get("id"),
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name"),
                "chat_type": chat.get("type"),
            },
        )
    ]


__all__ = ["normalize_telegram_update"]
