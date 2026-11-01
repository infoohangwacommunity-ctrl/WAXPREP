"""Wax Prep domain models.

The student's world is one unified, timestamped history. Everything else
(notebook, summaries, knowledge, embeddings) is derived from it and points
back to it. There is no separate chat-session concept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from waxprep.domain.identifiers import WaxId


class StudentStatus(StrEnum):
    """Minimal account lifecycle status."""

    ACTIVE = "active"
    DISABLED = "disabled"


class AuthorKind(StrEnum):
    """Who produced a history entry (a provenance fact, not a topic)."""

    STUDENT = "student"
    TUTOR = "tutor"
    SYSTEM = "system"


class ObjectType(StrEnum):
    """Kinds of stored objects that can be indexed, cited, or traced."""

    HISTORY = "history"
    SUMMARY = "summary"
    NOTEBOOK = "notebook"
    KNOWLEDGE_NODE = "knowledge_node"
    KNOWLEDGE_EDGE = "knowledge_edge"
    ARTIFACT_VERSION = "artifact_version"
    ARTIFACT_CONTENT = "artifact_content"


# Object types that can be found by words or by meaning.
INDEXED_OBJECT_TYPES: frozenset[ObjectType] = frozenset(
    {
        ObjectType.HISTORY,
        ObjectType.SUMMARY,
        ObjectType.NOTEBOOK,
        ObjectType.KNOWLEDGE_NODE,
        ObjectType.ARTIFACT_VERSION,
        ObjectType.ARTIFACT_CONTENT,
    }
)


class NotebookEntryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class SummaryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class OutboundStatus(StrEnum):
    """Lifecycle of an outbound channel delivery attempt.

    The outbox pattern: the tutor reply is saved first, then a delivery
    record is created, then delivery is attempted. A crash between
    saving the reply and marking delivered is recoverable: the retry
    sends the same stored reply without invoking the Teacher again.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    FAILED = "failed"


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
class HistoryEntry:
    """One thing that happened in a student's world (message in or out).

    ``occurred_at`` is when it happened; ``recorded_at`` is when we saved it.
    ``thread_ref`` is a purely technical channel detail and never meaning.
    """

    id: UUID
    wax_id: WaxId
    author_kind: AuthorKind
    content_kind: str
    text: str | None
    occurred_at: datetime
    recorded_at: datetime
    channel: str | None = None
    channel_ref: str | None = None
    thread_ref: str | None = None
    reply_to_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    """Media/document metadata linked to a history entry (bytes elsewhere)."""

    id: UUID
    wax_id: WaxId
    history_entry_id: UUID
    media_category: str
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
    """Durable understanding derived from history; payload is open-ended."""

    id: UUID
    wax_id: WaxId
    notebook_id: UUID
    schema_version: int
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    status: NotebookEntryStatus = NotebookEntryStatus.ACTIVE
    superseded_by_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Summary:
    """A compressed view of a span of history. Never replaces the history."""

    id: UUID
    wax_id: WaxId
    text: str
    covers_from: datetime
    covers_until: datetime
    level: int
    produced_by: str
    created_at: datetime
    status: SummaryStatus = SummaryStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class ProvenanceLink:
    """Says: this derived object is supported by that source object."""

    id: UUID
    wax_id: WaxId
    derived_type: ObjectType
    derived_id: UUID
    source_type: ObjectType
    source_id: UUID
    created_at: datetime
    source_author_kind: AuthorKind | None = None
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    """A retrieval vector for one stored object (an index, not memory)."""

    id: UUID
    wax_id: WaxId
    object_type: ObjectType
    object_id: UUID
    object_time: datetime
    model: str
    vector: tuple[float, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DerivationJob:
    """Background work that can fail and retry without touching history."""

    id: UUID
    wax_id: WaxId
    kind: str
    object_type: ObjectType
    object_id: UUID
    status: JobStatus
    attempts: int
    run_after: datetime
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    lease_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class OutboundDelivery:
    """One outbound delivery attempt for a tutor reply (outbox).

    The tutor reply is saved to ``history_entries`` first; this record
    tracks the channel delivery of that reply. A crash between saving
    the reply and marking this delivered is recoverable: the retry
    sends the same stored reply without invoking the Teacher again.

    ``provider_message_id`` is the WhatsApp message ID returned when the
    provider accepts the message. It is persisted so delivery-status
    callbacks can be matched to the correct outbound record.
    """

    id: UUID
    wax_id: WaxId
    tutor_entry_id: UUID
    channel: str | None
    channel_ref: str | None
    status: OutboundStatus
    attempts: int
    last_error: str | None
    delivered_at: datetime | None
    run_after: datetime
    created_at: datetime
    updated_at: datetime
    provider_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """Text-like representation extracted from an artifact version."""

    id: UUID
    wax_id: WaxId
    artifact_version_id: UUID
    representation: str
    text: str
    produced_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InvestigationRecord:
    """Audit record of one Context Intelligence investigation."""

    id: UUID
    wax_id: WaxId
    trigger_entry_id: UUID | None
    input_text: str
    result: dict[str, Any]
    model: str
    created_at: datetime


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
    "INDEXED_OBJECT_TYPES",
    "Artifact",
    "ArtifactContent",
    "ArtifactReference",
    "ArtifactStatus",
    "ArtifactVersion",
    "Attachment",
    "AuthorKind",
    "ChannelIdentity",
    "DerivationJob",
    "EmbeddingRecord",
    "Event",
    "HistoryEntry",
    "InvestigationRecord",
    "JobStatus",
    "Notebook",
    "NotebookEntry",
    "NotebookEntryStatus",
    "ObjectType",
    "ProvenanceLink",
    "Student",
    "StudentStatus",
    "Summary",
    "SummaryStatus",
    "Workspace",
    "new_id",
]
