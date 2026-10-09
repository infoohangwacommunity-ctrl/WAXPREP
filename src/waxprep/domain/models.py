"""Wax Prep domain models.

Build 2: identity, conversation, attachments, open-ended notebook.
Build 2.5: Workspace/Artifact substrate (no tutoring intelligence).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from waxprep.domain.identifiers import WaxId


class MessageContentType(StrEnum):
    """Supported message content categories."""

    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"


class StudentStatus(StrEnum):
    """Minimal account lifecycle status."""

    ACTIVE = "active"
    DISABLED = "disabled"


class ArtifactStatus(StrEnum):
    """Lifecycle state of a durable workspace artifact."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    PENDING_DELETION = "pending_deletion"
    DELETED = "deleted"


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
    """Media/document metadata linked to a message (bytes outside PostgreSQL)."""

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
    """Open-ended notebook container for one WAX ID."""

    id: UUID
    wax_id: WaxId
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotebookEntry:
    """One open-ended notebook entry with unconstrained payload."""

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
class Workspace:
    """Durable educational storage area belonging to one WAX ID."""

    id: UUID
    wax_id: WaxId
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Artifact:
    """Logical identity of a durable educational artifact."""

    id: UUID
    wax_id: WaxId
    workspace_id: UUID
    status: ArtifactStatus
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    """One immutable stored version of an artifact."""

    id: UUID
    wax_id: WaxId
    artifact_id: UUID
    version_number: int
    storage_ref: str
    original_filename: str | None
    mime_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Relationship between an artifact and another Wax Prep object."""

    id: UUID
    wax_id: WaxId
    artifact_id: UUID
    reference_type: str
    reference_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Event:
    """Minimal durable domain event."""

    id: UUID
    wax_id: WaxId | None
    kind: str
    object_type: str | None
    object_id: UUID | None
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


def new_id() -> UUID:
    """Create a new opaque UUID."""
    return uuid4()


__all__ = [
    "Artifact",
    "ArtifactReference",
    "ArtifactStatus",
    "ArtifactVersion",
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
    "Workspace",
    "new_id",
]
