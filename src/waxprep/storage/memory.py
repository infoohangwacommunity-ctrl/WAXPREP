"""In-memory storage for unit tests (not production)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactReference,
    ArtifactVersion,
    Attachment,
    ChannelIdentity,
    Conversation,
    Event,
    Message,
    Notebook,
    NotebookEntry,
    Student,
    Workspace,
    new_id,
)


class InMemoryStorage:
    """Deterministic in-process implementation of the Storage contract."""

    def __init__(self) -> None:
        self._students: dict[WaxId, Student] = {}
        self._channels: dict[tuple[str, str], ChannelIdentity] = {}
        self._conversations: dict[tuple[WaxId, UUID], Conversation] = {}
        self._messages: dict[tuple[WaxId, UUID, UUID], Message] = {}
        self._attachments: dict[tuple[WaxId, UUID], Attachment] = {}
        self._notebooks: dict[WaxId, Notebook] = {}
        self._entries: dict[tuple[WaxId, UUID], list[NotebookEntry]] = {}
        self._workspaces: dict[WaxId, Workspace] = {}
        self._artifacts: dict[tuple[WaxId, UUID], Artifact] = {}
        self._artifact_versions: dict[tuple[WaxId, UUID, UUID], ArtifactVersion] = {}
        self._artifact_references: dict[
            tuple[WaxId, UUID], list[ArtifactReference]
        ] = {}
        self._events: dict[WaxId, list[Event]] = {}

    def create_student(self, student: Student) -> Student:
        if student.wax_id in self._students:
            raise ValueError("student already exists")
        self._students[student.wax_id] = student
        return student

    def get_student(self, wax_id: WaxId) -> Student | None:
        return self._students.get(wax_id)

    def create_channel_identity(self, identity: ChannelIdentity) -> ChannelIdentity:
        key = (identity.channel, identity.external_id)
        if key in self._channels:
            raise ValueError("channel identity already exists")
        if identity.wax_id not in self._students:
            raise ValueError("unknown student")
        self._channels[key] = identity
        return identity

    def get_channel_identity(
        self, channel: str, external_id: str
    ) -> ChannelIdentity | None:
        return self._channels.get((channel, external_id))

    def create_conversation(self, conversation: Conversation) -> Conversation:
        if conversation.wax_id not in self._students:
            raise ValueError("unknown student")
        key = (conversation.wax_id, conversation.id)
        if key in self._conversations:
            raise ValueError("conversation already exists")
        self._conversations[key] = conversation
        return conversation

    def get_conversation(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> Conversation | None:
        return self._conversations.get((wax_id, conversation_id))

    def create_message(self, message: Message) -> Message:
        if (message.wax_id, message.conversation_id) not in self._conversations:
            raise ValueError("unknown conversation for student")
        key = (message.wax_id, message.conversation_id, message.id)
        if key in self._messages:
            raise ValueError("message already exists")
        self._messages[key] = message
        return message

    def get_message(
        self, wax_id: WaxId, conversation_id: UUID, message_id: UUID
    ) -> Message | None:
        return self._messages.get((wax_id, conversation_id, message_id))

    def list_all_messages(self, wax_id: WaxId) -> tuple[Message, ...]:
        rows = [m for (w, _, _), m in self._messages.items() if w == wax_id]
        return tuple(sorted(rows, key=lambda m: m.created_at))

    def list_messages(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> tuple[Message, ...]:
        rows = [
            m
            for (w, c, _), m in self._messages.items()
            if w == wax_id and c == conversation_id
        ]
        return tuple(sorted(rows, key=lambda m: m.created_at))

    def create_attachment(self, attachment: Attachment) -> Attachment:
        found = any(
            m.id == attachment.message_id and m.wax_id == attachment.wax_id
            for m in self._messages.values()
        )
        if not found:
            raise ValueError("unknown message for student")
        key = (attachment.wax_id, attachment.id)
        if key in self._attachments:
            raise ValueError("attachment already exists")
        self._attachments[key] = attachment
        return attachment

    def get_attachment(self, wax_id: WaxId, attachment_id: UUID) -> Attachment | None:
        return self._attachments.get((wax_id, attachment_id))

    def get_or_create_notebook(self, wax_id: WaxId, now: object) -> Notebook:
        existing = self._notebooks.get(wax_id)
        if existing is not None:
            return existing
        if not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        if wax_id not in self._students:
            raise ValueError("unknown student")
        notebook = Notebook(
            id=new_id(),
            wax_id=wax_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._notebooks[wax_id] = notebook
        self._entries[(wax_id, notebook.id)] = []
        return notebook

    def get_notebook(self, wax_id: WaxId) -> Notebook | None:
        return self._notebooks.get(wax_id)

    def add_notebook_entry(self, entry: NotebookEntry) -> NotebookEntry:
        notebook = self._notebooks.get(entry.wax_id)
        if notebook is None or notebook.id != entry.notebook_id:
            raise ValueError("unknown notebook for student")
        self._entries.setdefault((entry.wax_id, entry.notebook_id), []).append(entry)
        return entry

    def list_notebook_entries(
        self, wax_id: WaxId, notebook_id: UUID
    ) -> tuple[NotebookEntry, ...]:
        return tuple(self._entries.get((wax_id, notebook_id), []))

    def get_or_create_workspace(self, wax_id: WaxId, now: datetime) -> Workspace:
        existing = self._workspaces.get(wax_id)
        if existing is not None:
            return existing
        if wax_id not in self._students:
            raise ValueError("unknown student")
        workspace = Workspace(
            id=new_id(), wax_id=wax_id, created_at=now, updated_at=now
        )
        self._workspaces[wax_id] = workspace
        return workspace

    def get_workspace(self, wax_id: WaxId) -> Workspace | None:
        return self._workspaces.get(wax_id)

    def create_artifact(self, artifact: Artifact) -> Artifact:
        workspace = self._workspaces.get(artifact.wax_id)
        if workspace is None or workspace.id != artifact.workspace_id:
            raise ValueError("unknown workspace for student")
        key = (artifact.wax_id, artifact.id)
        if key in self._artifacts:
            raise ValueError("artifact already exists")
        self._artifacts[key] = artifact
        return artifact

    def get_artifact(self, wax_id: WaxId, artifact_id: UUID) -> Artifact | None:
        return self._artifacts.get((wax_id, artifact_id))

    def list_artifacts(self, wax_id: WaxId, workspace_id: UUID) -> tuple[Artifact, ...]:
        artifacts = [
            a
            for (owner, _), a in self._artifacts.items()
            if owner == wax_id and a.workspace_id == workspace_id
        ]
        return tuple(sorted(artifacts, key=lambda a: a.created_at))

    def create_artifact_version(self, version: ArtifactVersion) -> ArtifactVersion:
        artifact = self._artifacts.get((version.wax_id, version.artifact_id))
        if artifact is None:
            raise ValueError("unknown artifact for student")
        if version.version_number < 1:
            raise ValueError("version_number must be >= 1")
        existing = self.list_artifact_versions(version.wax_id, version.artifact_id)
        if any(item.version_number == version.version_number for item in existing):
            raise ValueError("artifact version number already exists")
        key = (version.wax_id, version.artifact_id, version.id)
        if key in self._artifact_versions:
            raise ValueError("artifact version already exists")
        self._artifact_versions[key] = version
        return version

    def get_artifact_version(
        self, wax_id: WaxId, artifact_id: UUID, version_id: UUID
    ) -> ArtifactVersion | None:
        return self._artifact_versions.get((wax_id, artifact_id, version_id))

    def list_artifact_versions(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactVersion, ...]:
        versions = [
            v
            for (owner, art, _), v in self._artifact_versions.items()
            if owner == wax_id and art == artifact_id
        ]
        return tuple(sorted(versions, key=lambda v: v.version_number))

    def set_current_artifact_version(
        self,
        wax_id: WaxId,
        artifact_id: UUID,
        version_id: UUID,
        updated_at: datetime,
    ) -> Artifact:
        artifact = self._artifacts.get((wax_id, artifact_id))
        if artifact is None:
            raise ValueError("unknown artifact")
        version = self.get_artifact_version(wax_id, artifact_id, version_id)
        if version is None:
            raise ValueError("unknown artifact version")
        updated = Artifact(
            id=artifact.id,
            wax_id=artifact.wax_id,
            workspace_id=artifact.workspace_id,
            status=artifact.status,
            current_version_id=version.id,
            created_at=artifact.created_at,
            updated_at=updated_at,
        )
        self._artifacts[(wax_id, artifact_id)] = updated
        return updated

    def create_artifact_reference(
        self, reference: ArtifactReference
    ) -> ArtifactReference:
        artifact = self._artifacts.get((reference.wax_id, reference.artifact_id))
        if artifact is None:
            raise ValueError("unknown artifact for student")
        key = (reference.wax_id, reference.artifact_id)
        references = self._artifact_references.setdefault(key, [])
        if any(
            item.reference_type == reference.reference_type
            and item.reference_id == reference.reference_id
            for item in references
        ):
            raise ValueError("artifact reference already exists")
        references.append(reference)
        return reference

    def list_artifact_references(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactReference, ...]:
        return tuple(self._artifact_references.get((wax_id, artifact_id), []))

    def append_event(self, event: Event) -> Event:
        if event.wax_id is None:
            return event
        self._events.setdefault(event.wax_id, []).append(event)
        return event

    def list_events(self, wax_id: WaxId) -> tuple[Event, ...]:
        return tuple(self._events.get(wax_id, []))


__all__ = ["InMemoryStorage"]
