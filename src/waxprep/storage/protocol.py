"""Storage contracts — ownership-scoped APIs only.

There is deliberately no way to delete or edit a history entry here.
History is the ground truth; age changes how easy something is to find,
never whether it exists.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactContent,
    ArtifactReference,
    ArtifactVersion,
    Attachment,
    ChannelIdentity,
    DerivationJob,
    EmbeddingRecord,
    Event,
    HistoryEntry,
    InteractiveOffer,
    InvestigationRecord,
    Notebook,
    NotebookEntry,
    ObjectType,
    OutboundDelivery,
    ProvenanceLink,
    Student,
    Summary,
    Workspace,
)
from waxprep.knowledge.models import KnowledgeEdge, KnowledgeNode


@dataclass(frozen=True, slots=True)
class TokenMatch:
    """One blind-index hit: this object contains this (hashed) word."""

    object_type: ObjectType
    object_id: UUID
    object_time: datetime
    token_hash: bytes


@dataclass(frozen=True, slots=True)
class TokenStats:
    """How common each hashed word is inside one student's index."""

    total_objects: int
    document_frequency: dict[bytes, int]


class Storage(Protocol):
    """Student-scoped persistence. Cross-student access must fail."""

    def transaction(self) -> AbstractContextManager[Storage]:
        """Context manager that wraps multiple storage operations in one
        database transaction. If any operation within the context raises,
        the whole transaction rolls back — no partial state.

        For derived-object writes (node + provenance + index + jobs),
        use this to make the logical mutation atomic (spec section 9).
        """
        ...

    # --- identity -------------------------------------------------------
    def create_student(self, student: Student) -> Student: ...

    def get_student(self, wax_id: WaxId) -> Student | None: ...

    def create_channel_identity(self, identity: ChannelIdentity) -> ChannelIdentity: ...

    def get_channel_identity(
        self, channel: str, external_id: str
    ) -> ChannelIdentity | None: ...

    # --- unified history ------------------------------------------------
    def append_history_entry(self, entry: HistoryEntry) -> HistoryEntry:
        """Save an entry. Same (channel, channel_ref) returns the saved one."""
        ...

    def append_history_entry_with_jobs(
        self,
        entry: HistoryEntry,
        token_hashes: tuple[bytes, ...],
        jobs: tuple[DerivationJob, ...],
    ) -> tuple[HistoryEntry, bool]:
        """Atomically persist a history entry, its blind word index, and its
        initial derivation jobs in one transaction.

        Returns ``(saved_entry, created)``. ``created`` is ``False`` if a
        duplicate ``(channel, channel_ref)`` was already saved — the saved
        entry is returned and no jobs are enqueued in that case. If the
        transaction fails, the channel adapter must allow a provider retry
        (Part 8 Blocker C).
        """
        ...

    def get_history_entry(
        self, wax_id: WaxId, entry_id: UUID
    ) -> HistoryEntry | None: ...

    def list_history(
        self,
        wax_id: WaxId,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> tuple[HistoryEntry, ...]: ...

    def list_history_around(
        self, wax_id: WaxId, entry_id: UUID, *, before: int, after: int
    ) -> tuple[HistoryEntry, ...]: ...

    def find_history_by_channel_ref(
        self, wax_id: WaxId, channel: str, channel_ref: str
    ) -> HistoryEntry | None:
        """Lookup one history entry by (wax_id, channel, channel_ref).

        Uses the unique partial index on (wax_id, channel, channel_ref)
        instead of scanning up to 500 entries. Returns None when not found.
        """
        ...

    def list_unsummarized_history(
        self, wax_id: WaxId, *, before: datetime, limit: int
    ) -> tuple[HistoryEntry, ...]: ...

    # --- attachments ----------------------------------------------------
    def create_attachment(self, attachment: Attachment) -> Attachment: ...

    def get_attachment(
        self, wax_id: WaxId, attachment_id: UUID
    ) -> Attachment | None: ...

    def list_attachments_for_entry(
        self, wax_id: WaxId, entry_id: UUID
    ) -> tuple[Attachment, ...]: ...

    # --- search index (words and meaning) --------------------------------
    def put_search_tokens(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        object_time: datetime,
        token_hashes: tuple[bytes, ...],
    ) -> None: ...

    def token_stats(
        self,
        wax_id: WaxId,
        token_hashes: tuple[bytes, ...],
        object_types: tuple[ObjectType, ...],
    ) -> TokenStats: ...

    def find_by_tokens(
        self,
        wax_id: WaxId,
        token_hashes: tuple[bytes, ...],
        *,
        object_types: tuple[ObjectType, ...],
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> tuple[TokenMatch, ...]:
        """Find token matches. ``limit=None`` means the whole corpus.

        For paginated retrieval use ``find_by_tokens_page`` instead —
        it returns one deterministic page and a cursor for the next.
        """
        ...

    def find_by_tokens_page(
        self,
        wax_id: WaxId,
        token_hashes: tuple[bytes, ...],
        *,
        object_types: tuple[ObjectType, ...],
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        cursor: tuple[datetime, UUID, bytes] | None = None,
    ) -> tuple[TokenMatch, ...]:
        """One page of token matches in deterministic order.

        The cursor is ``(object_time, object_id, token_hash)`` of the
        last row from the previous page. Rows are ordered
        ``object_time DESC, object_id, token_hash`` so the next page
        returns rows strictly after the cursor. An empty tuple means
        no more pages.
        """
        ...

    def put_embedding(self, record: EmbeddingRecord) -> None: ...

    def list_embeddings(
        self,
        wax_id: WaxId,
        object_types: tuple[ObjectType, ...],
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> tuple[EmbeddingRecord, ...]:
        """List embeddings. ``limit=None`` means the whole corpus."""
        ...

    def list_embeddings_page(
        self,
        wax_id: WaxId,
        object_types: tuple[ObjectType, ...],
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[EmbeddingRecord, ...]:
        """One page of embeddings in deterministic order.

        The cursor is ``(object_time, object_id)`` of the last row from
        the previous page. Rows are ordered ``object_time DESC, object_id``
        so the next page returns rows strictly after the cursor. An
        empty tuple means no more pages.
        """
        ...

    # --- notebook (derived durable understanding) --------------------------
    def get_or_create_notebook(self, wax_id: WaxId, now: datetime) -> Notebook: ...

    def get_notebook(self, wax_id: WaxId) -> Notebook | None: ...

    def add_notebook_entry(self, entry: NotebookEntry) -> NotebookEntry: ...

    def get_notebook_entry(
        self, wax_id: WaxId, entry_id: UUID
    ) -> NotebookEntry | None: ...

    def list_notebook_entries(
        self, wax_id: WaxId, notebook_id: UUID, *, include_superseded: bool = False
    ) -> tuple[NotebookEntry, ...]: ...

    def supersede_notebook_entry(
        self, wax_id: WaxId, old_id: UUID, new_id: UUID, now: datetime
    ) -> NotebookEntry: ...

    # --- summaries --------------------------------------------------------
    def add_summary(self, summary: Summary) -> Summary: ...

    def get_summary(self, wax_id: WaxId, summary_id: UUID) -> Summary | None: ...

    def list_summaries(
        self, wax_id: WaxId, *, include_superseded: bool = False, limit: int = 50
    ) -> tuple[Summary, ...]: ...

    def supersede_summary(
        self, wax_id: WaxId, summary_id: UUID, now: datetime
    ) -> Summary: ...

    # --- provenance ---------------------------------------------------------
    def add_provenance_links(self, links: tuple[ProvenanceLink, ...]) -> None: ...

    def list_provenance(
        self, wax_id: WaxId, derived_type: ObjectType, derived_id: UUID
    ) -> tuple[ProvenanceLink, ...]: ...

    def list_derived_from(
        self, wax_id: WaxId, source_type: ObjectType, source_id: UUID
    ) -> tuple[ProvenanceLink, ...]: ...

    # --- knowledge graph (derived relationships) -----------------------------
    def save_node(self, node: KnowledgeNode) -> KnowledgeNode: ...

    def get_node(self, wax_id: WaxId, node_id: UUID) -> KnowledgeNode | None: ...

    def add_edge(self, edge: KnowledgeEdge) -> KnowledgeEdge: ...

    def get_edge(self, wax_id: WaxId, edge_id: UUID) -> KnowledgeEdge | None: ...

    def list_edges(
        self, wax_id: WaxId, node_id: UUID, *, direction: str, limit: int
    ) -> tuple[KnowledgeEdge, ...]: ...

    # --- workspace and artifacts ------------------------------------------------
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

    def get_artifact_version_by_id(
        self, wax_id: WaxId, version_id: UUID
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

    def add_artifact_content(self, content: ArtifactContent) -> ArtifactContent: ...

    def get_artifact_content(
        self, wax_id: WaxId, content_id: UUID
    ) -> ArtifactContent | None: ...

    def list_artifact_content(
        self, wax_id: WaxId, artifact_version_id: UUID
    ) -> tuple[ArtifactContent, ...]: ...

    # --- background work ------------------------------------------------------------
    def enqueue_job(self, job: DerivationJob) -> DerivationJob: ...

    def list_wax_ids_with_due_jobs(
        self, now: datetime, limit: int
    ) -> tuple[WaxId, ...]: ...

    def find_tutor_reply_to(
        self, wax_id: WaxId, student_entry_id: UUID
    ) -> HistoryEntry | None: ...

    def claim_due_jobs(
        self, wax_id: WaxId, now: datetime, limit: int
    ) -> tuple[DerivationJob, ...]: ...

    def release_job_capability_skip(
        self, wax_id: WaxId, job_id: UUID, now: datetime, retry_at: datetime
    ) -> None: ...

    def mark_job_done(self, wax_id: WaxId, job_id: UUID, now: datetime) -> None: ...

    def mark_job_failed(
        self,
        wax_id: WaxId,
        job_id: UUID,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> None: ...

    # --- outbound delivery (outbox) ------------------------------------------------
    def create_outbound_delivery(
        self, delivery: OutboundDelivery
    ) -> OutboundDelivery: ...

    def get_outbound_delivery_for_tutor(
        self, wax_id: WaxId, tutor_entry_id: UUID, channel: str | None
    ) -> OutboundDelivery | None: ...

    def list_due_outbound_deliveries(
        self, wax_id: WaxId | None, now: datetime, limit: int
    ) -> tuple[OutboundDelivery, ...]: ...

    def mark_outbound_delivered(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        provider_message_id: str | None = None,
    ) -> None: ...

    def mark_outbound_failed(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> None: ...

    # --- interactive offers (one-shot buttons) --------------------------------------
    def create_interactive_offer(self, offer: InteractiveOffer) -> InteractiveOffer: ...

    def find_interactive_offer_by_button_id(
        self, wax_id: WaxId, button_id: str
    ) -> InteractiveOffer | None: ...

    def consume_interactive_offer(
        self,
        wax_id: WaxId,
        offer_id: UUID,
        button_id: str,
        now: datetime,
    ) -> InteractiveOffer | None:
        """Atomically mark offer consumed if still open. Returns None if not open."""
        ...

    # --- audit ------------------------------------------------------------------------
    def record_investigation(
        self, record: InvestigationRecord
    ) -> InvestigationRecord: ...

    def append_event(self, event: Event) -> Event: ...

    def list_events(self, wax_id: WaxId) -> tuple[Event, ...]: ...


__all__ = ["Storage", "TokenMatch", "TokenStats"]
