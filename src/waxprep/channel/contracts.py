"""Normalized channel contracts — provider-neutral inbound and outbound."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId


@dataclass(frozen=True, slots=True)
class InboundAttachmentDescriptor:
    provider_media_id: str
    claimed_mime: str | None
    original_filename: str | None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedInbound:
    channel: str
    provider_sender_id: str
    wax_id: WaxId | None
    provider_message_id: str
    occurred_at: datetime
    text: str | None
    reply_to_provider_id: str | None = None
    attachments: tuple[InboundAttachmentDescriptor, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundPayload:
    channel: str
    wax_id: WaxId
    recipient_external_id: str
    text: str | None
    artifact_storage_refs: tuple[str, ...] = ()
    tutor_entry_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OutboundResult:
    success: bool
    provider_message_id: str | None
    temporary_failure: bool
    error_code: str | None = None
    error_detail: str | None = None
