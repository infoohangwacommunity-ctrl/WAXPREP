"""Normalized channel contracts — provider-neutral inbound and outbound."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId

# Stable content kinds for history / CI. Teaching brain never branches on
# raw Meta JSON; these labels are infrastructure vocabulary only.
CONTENT_KIND_TEXT = "text"
CONTENT_KIND_IMAGE = "image"
CONTENT_KIND_AUDIO = "audio"
CONTENT_KIND_VIDEO = "video"
CONTENT_KIND_DOCUMENT = "document"
CONTENT_KIND_STICKER = "sticker"
CONTENT_KIND_LOCATION = "location"
CONTENT_KIND_CONTACTS = "contacts"
CONTENT_KIND_REACTION = "reaction"
CONTENT_KIND_INTERACTIVE_BUTTON = "interactive_button"
CONTENT_KIND_INTERACTIVE_LIST = "interactive_list"
CONTENT_KIND_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class InboundAttachmentDescriptor:
    provider_media_id: str
    claimed_mime: str | None
    original_filename: str | None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedInbound:
    """Provider-neutral inbound message after WhatsApp (or other) parsing.

    Every supported and unsupported type becomes one durable history candidate.
    ``text`` is always a best-effort human-readable summary for indexing and CI.
    ``structured`` holds type-specific fields (emoji, button id, lat/lng, etc.).
    """

    channel: str
    provider_sender_id: str
    wax_id: WaxId | None
    provider_message_id: str
    occurred_at: datetime
    text: str | None
    content_kind: str = CONTENT_KIND_TEXT
    reply_to_provider_id: str | None = None
    attachments: tuple[InboundAttachmentDescriptor, ...] = ()
    structured: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InteractiveButtonSpec:
    """One reply button the Teacher may suggest (opaque id assigned at send)."""

    title: str
    # Optional caller-supplied id; channel layer always issues opaque ids
    # when sending so product code never uses semantic button ontology.
    id_hint: str | None = None


@dataclass(frozen=True, slots=True)
class InteractiveSuggestion:
    """Optional interactive presentation chosen by the Teacher.

    Body teaching always lives in reply_text. Buttons are optional UX only.
    Max 3 reply buttons; titles should be short (Meta limit 20 chars).
    """

    body_text: str
    buttons: tuple[InteractiveButtonSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class OutboundPayload:
    channel: str
    wax_id: WaxId
    recipient_external_id: str
    text: str | None
    artifact_storage_refs: tuple[str, ...] = ()
    tutor_entry_id: UUID | None = None
    interactive: InteractiveSuggestion | None = None


@dataclass(frozen=True, slots=True)
class OutboundResult:
    success: bool
    provider_message_id: str | None
    temporary_failure: bool
    error_code: str | None = None
    error_detail: str | None = None
