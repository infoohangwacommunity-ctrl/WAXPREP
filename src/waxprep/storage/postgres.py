"""PostgreSQL storage implementation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactReference,
    ArtifactStatus,
    ArtifactVersion,
    Attachment,
    ChannelIdentity,
    Conversation,
    Event,
    Message,
    MessageContentType,
    Notebook,
    NotebookEntry,
    Student,
    StudentStatus,
    Workspace,
    new_id,
)
from waxprep.storage.session import assume_app_role, set_current_wax_id


class PostgresStorage:
    """Ownership-scoped PostgreSQL storage."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn
        assume_app_role(conn)

    def create_student(self, student: Student) -> Student:
        set_current_wax_id(self._conn, student.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO students (wax_id, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (
                    student.wax_id,
                    student.status.value,
                    student.created_at,
                    student.updated_at,
                ),
            )
        self._conn.commit()
        return student

    def get_student(self, wax_id: WaxId) -> Student | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT wax_id, status, created_at, updated_at "
                "FROM students WHERE wax_id = %s",
                (wax_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Student(
            wax_id=WaxId(row["wax_id"]),
            status=StudentStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_channel_identity(self, identity: ChannelIdentity) -> ChannelIdentity:
        set_current_wax_id(self._conn, identity.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO channel_identities "
                "(id, wax_id, channel, external_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    identity.id,
                    identity.wax_id,
                    identity.channel,
                    identity.external_id,
                    identity.created_at,
                ),
            )
        self._conn.commit()
        return identity

    def get_channel_identity(
        self, channel: str, external_id: str
    ) -> ChannelIdentity | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, channel, external_id, created_at "
                "FROM waxprep_lookup_channel(%s, %s)",
                (channel, external_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ChannelIdentity(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            channel=row["channel"],
            external_id=row["external_id"],
            created_at=row["created_at"],
        )

    def create_conversation(self, conversation: Conversation) -> Conversation:
        set_current_wax_id(self._conn, conversation.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, wax_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (
                    conversation.id,
                    conversation.wax_id,
                    conversation.created_at,
                    conversation.updated_at,
                ),
            )
        self._conn.commit()
        return conversation

    def get_conversation(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> Conversation | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, created_at, updated_at FROM conversations "
                "WHERE wax_id = %s AND id = %s",
                (wax_id, conversation_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Conversation(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_message(self, message: Message) -> Message:
        set_current_wax_id(self._conn, message.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages "
                "(id, wax_id, conversation_id, role, content_type, "
                "text_body, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    message.id,
                    message.wax_id,
                    message.conversation_id,
                    message.role,
                    message.content_type.value,
                    message.text_body,
                    message.created_at,
                ),
            )
        self._conn.commit()
        return message

    def get_message(
        self, wax_id: WaxId, conversation_id: UUID, message_id: UUID
    ) -> Message | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, conversation_id, role, content_type, "
                "text_body, created_at FROM messages "
                "WHERE wax_id = %s AND conversation_id = %s AND id = %s",
                (wax_id, conversation_id, message_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Message(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            conversation_id=row["conversation_id"],
            role=row["role"],
            content_type=MessageContentType(row["content_type"]),
            text_body=row["text_body"],
            created_at=row["created_at"],
        )

    def list_messages(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> tuple[Message, ...]:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, conversation_id, role, content_type, "
                "text_body, created_at FROM messages "
                "WHERE wax_id = %s AND conversation_id = %s "
                "ORDER BY created_at ASC",
                (wax_id, conversation_id),
            )
            rows = cur.fetchall()
        return tuple(
            Message(
                id=r["id"],
                wax_id=WaxId(r["wax_id"]),
                conversation_id=r["conversation_id"],
                role=r["role"],
                content_type=MessageContentType(r["content_type"]),
                text_body=r["text_body"],
                created_at=r["created_at"],
            )
            for r in rows
        )

    def create_attachment(self, attachment: Attachment) -> Attachment:
        set_current_wax_id(self._conn, attachment.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO attachments "
                "(id, wax_id, message_id, media_category, mime_type, "
                "storage_ref, size_bytes, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    attachment.id,
                    attachment.wax_id,
                    attachment.message_id,
                    attachment.media_category.value,
                    attachment.mime_type,
                    attachment.storage_ref,
                    attachment.size_bytes,
                    attachment.created_at,
                ),
            )
        self._conn.commit()
        return attachment

    def get_attachment(self, wax_id: WaxId, attachment_id: UUID) -> Attachment | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, message_id, media_category, mime_type, "
                "storage_ref, size_bytes, created_at FROM attachments "
                "WHERE wax_id = %s AND id = %s",
                (wax_id, attachment_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Attachment(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            message_id=row["message_id"],
            media_category=MessageContentType(row["media_category"]),
            mime_type=row["mime_type"],
            storage_ref=row["storage_ref"],
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
        )

    def get_or_create_notebook(self, wax_id: WaxId, now: object) -> Notebook:
        set_current_wax_id(self._conn, wax_id)
        existing = self.get_notebook(wax_id)
        if existing is not None:
            return existing
        if not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        notebook = Notebook(
            id=new_id(),
            wax_id=wax_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notebooks "
                "(id, wax_id, version, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    notebook.id,
                    notebook.wax_id,
                    notebook.version,
                    notebook.created_at,
                    notebook.updated_at,
                ),
            )
        self._conn.commit()
        return notebook

    def get_notebook(self, wax_id: WaxId) -> Notebook | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, version, created_at, updated_at "
                "FROM notebooks WHERE wax_id = %s",
                (wax_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Notebook(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def add_notebook_entry(self, entry: NotebookEntry) -> NotebookEntry:
        set_current_wax_id(self._conn, entry.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notebook_entries "
                "(id, wax_id, notebook_id, schema_version, payload, "
                "source_type, source_ref, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)",
                (
                    entry.id,
                    entry.wax_id,
                    entry.notebook_id,
                    entry.schema_version,
                    json.dumps(entry.payload),
                    entry.source_type,
                    entry.source_ref,
                    entry.created_at,
                    entry.updated_at,
                ),
            )
        self._conn.commit()
        return entry

    def list_notebook_entries(
        self, wax_id: WaxId, notebook_id: UUID
    ) -> tuple[NotebookEntry, ...]:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, notebook_id, schema_version, payload, "
                "source_type, source_ref, created_at, updated_at "
                "FROM notebook_entries "
                "WHERE wax_id = %s AND notebook_id = %s "
                "ORDER BY created_at ASC",
                (wax_id, notebook_id),
            )
            rows = cur.fetchall()
        result: list[NotebookEntry] = []
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            result.append(
                NotebookEntry(
                    id=r["id"],
                    wax_id=WaxId(r["wax_id"]),
                    notebook_id=r["notebook_id"],
                    schema_version=r["schema_version"],
                    payload=dict(payload),
                    source_type=r["source_type"],
                    source_ref=r["source_ref"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )
        return tuple(result)

    def append_event(self, event: Event) -> Event:
        if event.wax_id is not None:
            set_current_wax_id(self._conn, event.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events "
                "(id, wax_id, kind, object_type, object_id, created_at, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    event.id,
                    event.wax_id,
                    event.kind,
                    event.object_type,
                    event.object_id,
                    event.created_at,
                    json.dumps(event.metadata),
                ),
            )
        self._conn.commit()
        return event

    def list_events(self, wax_id: WaxId) -> tuple[Event, ...]:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, kind, object_type, object_id, "
                "created_at, metadata FROM events "
                "WHERE wax_id = %s ORDER BY created_at ASC",
                (wax_id,),
            )
            rows = cur.fetchall()
        out: list[Event] = []
        for r in rows:
            meta = r["metadata"]
            if isinstance(meta, str):
                meta = json.loads(meta)
            out.append(
                Event(
                    id=r["id"],
                    wax_id=WaxId(r["wax_id"]) if r["wax_id"] else None,
                    kind=r["kind"],
                    object_type=r["object_type"],
                    object_id=r["object_id"],
                    created_at=r["created_at"],
                    metadata=dict(meta),
                )
            )
        return tuple(out)

    def get_or_create_workspace(self, wax_id: WaxId, now: datetime) -> Workspace:
        existing = self.get_workspace(wax_id)
        if existing is not None:
            return existing
        set_current_wax_id(self._conn, wax_id)
        workspace = Workspace(
            id=new_id(), wax_id=wax_id, created_at=now, updated_at=now
        )
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workspaces (id, wax_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (
                    workspace.id,
                    workspace.wax_id,
                    workspace.created_at,
                    workspace.updated_at,
                ),
            )
        self._conn.commit()
        return workspace

    def get_workspace(self, wax_id: WaxId) -> Workspace | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, created_at, updated_at "
                "FROM workspaces WHERE wax_id = %s",
                (wax_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Workspace(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_artifact(self, artifact: Artifact) -> Artifact:
        set_current_wax_id(self._conn, artifact.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artifacts "
                "(id, wax_id, workspace_id, status, current_version_id, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    artifact.id,
                    artifact.wax_id,
                    artifact.workspace_id,
                    artifact.status.value,
                    artifact.current_version_id,
                    artifact.created_at,
                    artifact.updated_at,
                ),
            )
        self._conn.commit()
        return artifact

    def get_artifact(self, wax_id: WaxId, artifact_id: UUID) -> Artifact | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, workspace_id, status, current_version_id, "
                "created_at, updated_at FROM artifacts "
                "WHERE wax_id = %s AND id = %s",
                (wax_id, artifact_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Artifact(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            workspace_id=row["workspace_id"],
            status=ArtifactStatus(row["status"]),
            current_version_id=row["current_version_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_artifacts(self, wax_id: WaxId, workspace_id: UUID) -> tuple[Artifact, ...]:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, workspace_id, status, current_version_id, "
                "created_at, updated_at FROM artifacts "
                "WHERE wax_id = %s AND workspace_id = %s "
                "ORDER BY created_at ASC",
                (wax_id, workspace_id),
            )
            rows = cur.fetchall()
        return tuple(
            Artifact(
                id=r["id"],
                wax_id=WaxId(r["wax_id"]),
                workspace_id=r["workspace_id"],
                status=ArtifactStatus(r["status"]),
                current_version_id=r["current_version_id"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        )

    def create_artifact_version(self, version: ArtifactVersion) -> ArtifactVersion:
        set_current_wax_id(self._conn, version.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artifact_versions "
                "(id, wax_id, artifact_id, version_number, storage_ref, "
                "original_filename, mime_type, size_bytes, checksum_sha256, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    version.id,
                    version.wax_id,
                    version.artifact_id,
                    version.version_number,
                    version.storage_ref,
                    version.original_filename,
                    version.mime_type,
                    version.size_bytes,
                    version.checksum_sha256,
                    version.created_at,
                ),
            )
        self._conn.commit()
        return version

    def get_artifact_version(
        self, wax_id: WaxId, artifact_id: UUID, version_id: UUID
    ) -> ArtifactVersion | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, artifact_id, version_number, storage_ref, "
                "original_filename, mime_type, size_bytes, checksum_sha256, "
                "created_at FROM artifact_versions "
                "WHERE wax_id = %s AND artifact_id = %s AND id = %s",
                (wax_id, artifact_id, version_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ArtifactVersion(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            artifact_id=row["artifact_id"],
            version_number=row["version_number"],
            storage_ref=row["storage_ref"],
            original_filename=row["original_filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            checksum_sha256=row["checksum_sha256"],
            created_at=row["created_at"],
        )

    def list_artifact_versions(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactVersion, ...]:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, artifact_id, version_number, storage_ref, "
                "original_filename, mime_type, size_bytes, checksum_sha256, "
                "created_at FROM artifact_versions "
                "WHERE wax_id = %s AND artifact_id = %s "
                "ORDER BY version_number ASC",
                (wax_id, artifact_id),
            )
            rows = cur.fetchall()
        return tuple(
            ArtifactVersion(
                id=r["id"],
                wax_id=WaxId(r["wax_id"]),
                artifact_id=r["artifact_id"],
                version_number=r["version_number"],
                storage_ref=r["storage_ref"],
                original_filename=r["original_filename"],
                mime_type=r["mime_type"],
                size_bytes=r["size_bytes"],
                checksum_sha256=r["checksum_sha256"],
                created_at=r["created_at"],
            )
            for r in rows
        )

    def set_current_artifact_version(
        self,
        wax_id: WaxId,
        artifact_id: UUID,
        version_id: UUID,
        updated_at: datetime,
    ) -> Artifact:
        set_current_wax_id(self._conn, wax_id)
        version = self.get_artifact_version(wax_id, artifact_id, version_id)
        if version is None:
            raise ValueError("unknown artifact version")
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE artifacts SET current_version_id = %s, updated_at = %s "
                "WHERE wax_id = %s AND id = %s",
                (version_id, updated_at, wax_id, artifact_id),
            )
        self._conn.commit()
        artifact = self.get_artifact(wax_id, artifact_id)
        if artifact is None:
            raise ValueError("unknown artifact")
        return artifact

    def create_artifact_reference(
        self, reference: ArtifactReference
    ) -> ArtifactReference:
        set_current_wax_id(self._conn, reference.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artifact_references "
                "(id, wax_id, artifact_id, reference_type, reference_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    reference.id,
                    reference.wax_id,
                    reference.artifact_id,
                    reference.reference_type,
                    reference.reference_id,
                    reference.created_at,
                ),
            )
        self._conn.commit()
        return reference

    def list_artifact_references(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactReference, ...]:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, artifact_id, reference_type, reference_id, "
                "created_at FROM artifact_references "
                "WHERE wax_id = %s AND artifact_id = %s "
                "ORDER BY created_at ASC",
                (wax_id, artifact_id),
            )
            rows = cur.fetchall()
        return tuple(
            ArtifactReference(
                id=r["id"],
                wax_id=WaxId(r["wax_id"]),
                artifact_id=r["artifact_id"],
                reference_type=r["reference_type"],
                reference_id=r["reference_id"],
                created_at=r["created_at"],
            )
            for r in rows
        )


__all__ = ["PostgresStorage"]
