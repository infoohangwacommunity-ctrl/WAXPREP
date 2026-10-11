"""Parse WhatsApp webhook JSON into NormalizedInbound messages.

Does not download media or call models. Durable accept happens after
normalization + identity resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from waxprep.channel.contracts import InboundAttachmentDescriptor, NormalizedInbound
from waxprep.domain.identifiers import WaxId


def normalize_webhook_payload(
    payload: dict[str, Any],
    *,
    resolve_wax_id: dict[str, WaxId] | None = None,
) -> tuple[NormalizedInbound, ...]:
    """Extract supported inbound messages from a Cloud API webhook body."""
    out: list[NormalizedInbound] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") != "messages":
                continue
            contacts = {
                c.get("wa_id"): c
                for c in (value.get("contacts") or [])
                if c.get("wa_id")
            }
            for msg in value.get("messages") or []:
                normalized = _one_message(msg, contacts, resolve_wax_id)
                if normalized is not None:
                    out.append(normalized)
    return tuple(out)


def _one_message(
    msg: dict[str, Any],
    contacts: dict[str, Any],
    resolve: dict[str, WaxId] | None,
) -> NormalizedInbound | None:
    sender = msg.get("from")
    mid = msg.get("id")
    if not sender or not mid:
        return None
    ts_raw = msg.get("timestamp")
    try:
        occurred = datetime.fromtimestamp(int(ts_raw or 0), tz=UTC)
    except (TypeError, ValueError, OSError):
        occurred = datetime.now(tz=UTC)
    mtype = msg.get("type")
    text: str | None = None
    attachments: list[InboundAttachmentDescriptor] = []
    if mtype == "text":
        text = (msg.get("text") or {}).get("body")
    elif mtype in {"image", "audio", "document", "video", "sticker"}:
        media = msg.get(mtype) or {}
        text = media.get("caption")
        attachments.append(
            InboundAttachmentDescriptor(
                provider_media_id=str(media.get("id") or ""),
                claimed_mime=media.get("mime_type"),
                original_filename=media.get("filename"),
            )
        )
    else:
        # Unsupported event types are ignored (statuses, reactions, etc.)
        return None
    wax = (resolve or {}).get(sender)
    context = msg.get("context") or {}
    return NormalizedInbound(
        channel="whatsapp",
        provider_sender_id=sender,
        wax_id=wax,
        provider_message_id=mid,
        occurred_at=occurred,
        text=text,
        reply_to_provider_id=context.get("id"),
        attachments=tuple(a for a in attachments if a.provider_media_id),
        metadata={"message_type": mtype},
    )
