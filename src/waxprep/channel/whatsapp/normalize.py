"""Parse WhatsApp webhook JSON into NormalizedInbound messages.

Does not download media or call models. Durable accept happens after
normalization + identity resolution.

Every inbound type becomes one durable history candidate with:
- content_kind (stable vocabulary)
- text (best-effort human-readable summary for index / CI)
- structured (type-specific JSON fields)

Unknown future types are saved, never dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from waxprep.channel.contracts import (
    CONTENT_KIND_AUDIO,
    CONTENT_KIND_CONTACTS,
    CONTENT_KIND_DOCUMENT,
    CONTENT_KIND_IMAGE,
    CONTENT_KIND_INTERACTIVE_BUTTON,
    CONTENT_KIND_INTERACTIVE_LIST,
    CONTENT_KIND_LOCATION,
    CONTENT_KIND_REACTION,
    CONTENT_KIND_STICKER,
    CONTENT_KIND_TEXT,
    CONTENT_KIND_UNKNOWN,
    CONTENT_KIND_VIDEO,
    InboundAttachmentDescriptor,
    NormalizedInbound,
)
from waxprep.domain.identifiers import WaxId

_MEDIA_KINDS = {
    "image": CONTENT_KIND_IMAGE,
    "audio": CONTENT_KIND_AUDIO,
    "video": CONTENT_KIND_VIDEO,
    "document": CONTENT_KIND_DOCUMENT,
    "sticker": CONTENT_KIND_STICKER,
}


def normalize_webhook_payload(
    payload: dict[str, Any],
    *,
    resolve_wax_id: dict[str, WaxId] | None = None,
) -> tuple[NormalizedInbound, ...]:
    """Extract inbound messages from a Cloud API webhook body.

    Status-only changes produce no messages (handled elsewhere).
    """
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

    mtype = str(msg.get("type") or "unknown")
    context = msg.get("context") or {}
    reply_to = context.get("id")
    wax = (resolve or {}).get(sender)

    text: str | None = None
    content_kind = CONTENT_KIND_UNKNOWN
    structured: dict[str, Any] = {}
    attachments: list[InboundAttachmentDescriptor] = []

    if mtype == "text":
        content_kind = CONTENT_KIND_TEXT
        text = (msg.get("text") or {}).get("body")
    elif mtype in _MEDIA_KINDS:
        content_kind = _MEDIA_KINDS[mtype]
        media = msg.get(mtype) or {}
        text = media.get("caption")
        media_id = str(media.get("id") or "")
        if media_id:
            attachments.append(
                InboundAttachmentDescriptor(
                    provider_media_id=media_id,
                    claimed_mime=media.get("mime_type"),
                    original_filename=media.get("filename"),
                )
            )
        if not text:
            text = f"[{content_kind}]"
        structured = {
            "provider_media_id": media_id or None,
            "mime_type": media.get("mime_type"),
            "filename": media.get("filename"),
        }
    elif mtype == "reaction":
        content_kind = CONTENT_KIND_REACTION
        reaction = msg.get("reaction") or {}
        emoji = reaction.get("emoji") or ""
        target_id = reaction.get("message_id") or ""
        text = f"Reacted {emoji} to message {target_id}".strip()
        structured = {"emoji": emoji, "target_message_id": target_id}
        # Reactions often include context pointing at the reacted message.
        if not reply_to and target_id:
            reply_to = target_id
    elif mtype == "location":
        content_kind = CONTENT_KIND_LOCATION
        loc = msg.get("location") or {}
        name = loc.get("name") or loc.get("address") or ""
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        text = name or f"Location ({lat}, {lng})"
        structured = {
            "latitude": lat,
            "longitude": lng,
            "name": loc.get("name"),
            "address": loc.get("address"),
        }
    elif mtype == "contacts":
        content_kind = CONTENT_KIND_CONTACTS
        contact_list = msg.get("contacts") or []
        names: list[str] = []
        for c in contact_list:
            n = (c.get("name") or {}).get("formatted_name") or ""
            if n:
                names.append(n)
        text = "Shared contact(s): " + (", ".join(names) if names else "unknown")
        structured = {"contacts": contact_list}
    elif mtype == "interactive":
        interactive = msg.get("interactive") or {}
        itype = interactive.get("type")
        if itype == "button_reply":
            content_kind = CONTENT_KIND_INTERACTIVE_BUTTON
            br = interactive.get("button_reply") or {}
            button_id = str(br.get("id") or "")
            title = str(br.get("title") or "")
            text = title or f"[button {button_id}]"
            structured = {
                "button_id": button_id,
                "title": title,
                "interactive_type": "button_reply",
            }
        elif itype == "list_reply":
            content_kind = CONTENT_KIND_INTERACTIVE_LIST
            lr = interactive.get("list_reply") or {}
            row_id = str(lr.get("id") or "")
            title = str(lr.get("title") or "")
            description = lr.get("description")
            text = title
            if description:
                text = f"{title}: {description}"
            structured = {
                "row_id": row_id,
                "title": title,
                "description": description,
                "interactive_type": "list_reply",
            }
        else:
            content_kind = CONTENT_KIND_UNKNOWN
            text = f"[unsupported interactive type: {itype}]"
            structured = {"interactive": interactive}
    elif mtype == "button":
        # Legacy quick-reply style (older Cloud API payloads).
        content_kind = CONTENT_KIND_INTERACTIVE_BUTTON
        button = msg.get("button") or {}
        title = str(button.get("text") or button.get("payload") or "")
        payload_id = str(button.get("payload") or "")
        text = title or f"[button {payload_id}]"
        structured = {
            "button_id": payload_id,
            "title": title,
            "interactive_type": "button",
        }
    else:
        # System, order, unsupported, future types — never drop.
        content_kind = CONTENT_KIND_UNKNOWN
        text = f"[unsupported message type: {mtype}]"
        structured = {"raw_type": mtype, "raw_keys": sorted(msg.keys())}

    return NormalizedInbound(
        channel="whatsapp",
        provider_sender_id=sender,
        wax_id=wax,
        provider_message_id=mid,
        occurred_at=occurred,
        text=text,
        content_kind=content_kind,
        reply_to_provider_id=reply_to,
        attachments=tuple(a for a in attachments if a.provider_media_id),
        structured=structured,
        metadata={"message_type": mtype, "contact": contacts.get(sender)},
    )
