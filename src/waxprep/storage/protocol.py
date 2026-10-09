"""Storage contracts — ownership-scoped APIs only."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
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
)


class Storage(Protocol):
    """Student-scoped persistence. Cross-student access must fail."""

    def create_student(self, student: Student) -> Student: ...

    def get_student(self, wax_id: WaxId) -> Student | None: ...

    def create_channel_identity(self, identity: ChannelIdentity) -> ChannelIdentity: ...

    def get_channel_identity(
        self, channel: str, external_id: str
    ) -> ChannelIdentity | None: ...

    def create_conversation(self, conversation: Conversation) -> Conversation: ...

    def get_conversation(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> Conversation | None: ...

    def create_message(self, message: Message) -> Message: ...

    def get_message(
        self, wax_id: WaxId, conversation_id: UUID, message_id: UUID
    ) -> Message | None: ...

    def list_messages(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> tuple[Message, ...]: ...

    def create_attachment(self, attachment: Attachment) -> Attachment: ...

    def get_attachment(
        self, wax_id: WaxId, attachment_id: UUID
    ) -> Attachment | None: ...

    def get_or_create_notebook(self, wax_id: WaxId, now: object) -> Notebook: ...

    def get_notebook(self, wax_id: WaxId) -> Notebook | None: ...

    def add_notebook_entry(self, entry: NotebookEntry) -> NotebookEntry: ...

    def list_notebook_entries(
        self, wax_id: WaxId, notebook_id: UUID
    ) -> tuple[NotebookEntry, ...]: ...

    def get_or_create_workspace(self, wax_id: WaxId, now: datetime) -> Workspace: ...

    def get_workspace(self, wax_id: WaxId) -> Workspace | None: ...

    def create_artifact(self, artifact: Artifact) -> Artifact: ...

    def get_artifact(self, wax_id: WaxId, artifact_id: UUID) -> Artifact | None: ...

    def list_artifacts(
        self, wax_id: WaxId, workspace_id: UUID
    ) -> tuple[Artifact, ...]: ...

    def create_artifact_version(self, version: ArtifactVersion) -> ArtifactVersion: ...

    def get_artifact_version(
        self, wax_id: WaxId, artifact_id: UUID, version_id: UUID
    ) -> ArtifactVersion | None: ...

    def list_artifact_versions(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactVersion, ...]: ...

    def set_current_artifact_version(
        self,
        wax_id: WaxId,
        artifact_id: UUID,
        version_id: UUID,
        updated_at: datetime,
    ) -> Artifact: ...

    def create_artifact_reference(
        self, reference: ArtifactReference
    ) -> ArtifactReference: ...

    def list_artifact_references(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactReference, ...]: ...

    def append_event(self, event: Event) -> Event: ...

    def list_events(self, wax_id: WaxId) -> tuple[Event, ...]: ...


__all__ = ["Storage"]
