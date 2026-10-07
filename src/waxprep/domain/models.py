"""Minimal domain models for Build 2 (storage contract, not tutoring logic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from waxprep.domain.identifiers import WaxId


class MessageContentType(StrEnum):
    """Supported message content categories (representation only)."""

    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"


class StudentStatus(StrEnum):
    """Minimal account lifecycle status."""

    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class Student:
    """Minimal account record — not a profile."""

    wax_id: WaxId
    created_at: datetime
    updated_at: datetime
    status: StudentStatus = StudentStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class ChannelIdentity:
    """Links an external channel identifier to a WAX ID."""

    id: UUID
    wax_id: WaxId
    channel: str
    external_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Conversation:
    """Durable conversation owned by exactly one WAX ID."""

    id: UUID
    wax_id: WaxId
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    """One message in a conversation."""

    id: UUID
    wax_id: WaxId
    conversation_id: UUID
    role: str
    content_type: MessageContentType
    text_body: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Attachment:
    """Media/document metadata linked to a message (blob stored elsewhere)."""

    id: UUID
    wax_id: WaxId
    message_id: UUID
    media_category: MessageContentType
    mime_type: str | None
    storage_ref: str
    size_bytes: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Notebook:
    """Open-ended notebook container for one WAX ID (not a profile form)."""

    id: UUID
    wax_id: WaxId
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotebookEntry:
    """One open-ended notebook entry with flexible structured payload.

    Semantic shape of ``payload`` is not constrained by Build 2.
    Technical fields enforce ownership, identity, and versioning only.
    """

    id: UUID
    wax_id: WaxId
    notebook_id: UUID
    schema_version: int
    payload: dict[str, Any]
    source_type: str | None
    source_ref: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Event:
    """Minimal durable domain event (no full message body duplication)."""

    id: UUID
    wax_id: WaxId | None
    kind: str
    object_type: str | None
    object_id: UUID | None
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


def new_id() -> UUID:
    return uuid4()


__all__ = [
    "Attachment",
    "ChannelIdentity",
    "Conversation",
    "Event",
    "Message",
    "MessageContentType",
    "Notebook",
    "NotebookEntry",
    "Student",
    "StudentStatus",
    "new_id",
]
