"""Storage contracts — ownership-scoped APIs only.

There is deliberately no way to delete or edit a history entry here.
History is the ground truth; age changes how easy something is to find,
never whether it exists.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactContent,
    ArtifactReference,
    ArtifactVersion,
    Attachment,
    CalendarEvent,
    ChannelIdentity,
    DeletionRequest,
    DerivationJob,
    EmbeddingRecord,
    Event,
    HistoryEntry,
    InteractiveOffer,
    InvestigationRecord,
    LearningState,
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


@dataclass(frozen=True, slots=True)
class ScoredMatch:
    """One similarity-search hit, scored against a query vector.

    Returned by ``Storage.find_by_meaning``. ``score`` is cosine
    similarity in ``[-1.0, 1.0]`` (1.0 == identical direction). When
    pgvector is available the score is computed in SQL by the ``<=>``
    operator (cosine distance); otherwise it is computed by the
    Python fallback ``cosine_similarity`` against each decrypted
    embedding.
    """

    object_type: ObjectType
    object_id: UUID
    object_time: datetime
    score: float


# Cursor for cursor-paginated history reads. The tuple is
# ``(occurred_at, seq)`` of the last row returned by the previous
# page; combined with ``wax_id`` it uniquely identifies a position in
# the history stream. ``None`` means "from the beginning".
HistoryCursor = tuple[datetime, int] | None


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

    def set_student_timezone(
        self, wax_id: WaxId, timezone: str | None, now: datetime
    ) -> Student | None:
        """Update the student's IANA timezone (Founder's vision §7).

        ``None`` clears the timezone. The Founder's brief is that the
        runtime must understand the student's local wall clock, so
        ``clock.student_local_now`` reads this column and converts the
        UTC ``now()`` accordingly. Never ask for the timezone upfront;
        it surfaces from context (e.g. "my exam is at 9am tomorrow")
        and is recorded once CI is confident.
        """
        ...

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

    def list_history_paginated(
        self,
        wax_id: WaxId,
        *,
        before: HistoryCursor = None,
        page_size: int = 25,
        newest_first: bool = True,
    ) -> tuple[tuple[HistoryEntry, ...], HistoryCursor]:
        """Return one page of history plus a cursor for the next page.

        ``before`` is the cursor returned by the previous call (the
        ``(occurred_at, seq)`` of the last row returned). Pass ``None``
        for the first page. ``newest_first=True`` walks the stream in
        reverse chronological order (the dominant access pattern for
        history UIs and the context retriever's browse_window). When
        the returned cursor is ``None`` the end of the stream has been
        reached.

        Uses the ``idx_history_wax_occurred_desc`` B-tree index from
        migration 016 for an index-only forward scan when
        ``newest_first=True``.

        RLS: enforced by ``assume_app_role`` + ``set_current_wax_id``
        on the underlying connection; the SQL never filters by
        ``wax_id`` directly — the policy does.
        """
        ...

    def search_history_by_keyword(
        self, wax_id: WaxId, query: str, *, limit: int = 50
    ) -> tuple[HistoryEntry, ...]:
        """Substring search over decrypted history text.

        ``history_entries.text_encrypted`` is AES-GCM ciphertext, so
        ILIKE cannot run server-side. This method fetches candidates
        in deterministic time order, decrypts each row, and applies
        a case-insensitive substring test in Python. Pagination stops
        as soon as ``limit`` matches are found or the stream is
        exhausted. For exact-word matches prefer
        ``find_by_tokens`` (blind HMAC index, server-side).

        RLS: ``assume_app_role`` + ``set_current_wax_id``. Only the
        rows the calling student owns are decrypted; the encryption
        key is derived per-student so even a key compromise of one
        student cannot read another's text.
        """
        ...

    def search_history_by_time_range(
        self,
        wax_id: WaxId,
        *,
        start_ts: datetime,
        end_ts: datetime,
        limit: int = 200,
    ) -> tuple[HistoryEntry, ...]:
        """All history in ``[start_ts, end_ts)`` for one student.

        Uses ``idx_history_wax_time_range`` (migration 016) for an
        index-only scan. ``limit`` caps the result size so a
        misconfigured range cannot trigger unbounded memory use.

        RLS: enforced by ``assume_app_role`` +
        ``set_current_wax_id``; the SQL filters by ``occurred_at``
        range only.
        """
        ...

    def has_pgvector(self) -> bool:
        """Return True iff the ``vector`` extension is loaded AND
        the ``search_embeddings.vector_plain`` column exists.

        When True, ``find_by_meaning`` uses pgvector's ``<=>`` ANN
        search (HNSW if the index was created, sequential scan
        otherwise). When False, the Python-side cosine fallback
        paginates the encrypted embeddings and decrypts each blob —
        the pre-pgvector behaviour.
        """
        ...

    def find_by_meaning(
        self,
        wax_id: WaxId,
        query_vector: tuple[float, ...],
        *,
        object_types: tuple[ObjectType, ...],
        since: datetime | None,
        until: datetime | None,
        limit: int = 8,
    ) -> tuple[ScoredMatch, ...]:
        """Semantic similarity search over one student's embeddings.

        Returns at most ``limit`` matches ranked by descending
        similarity to ``query_vector``. When ``has_pgvector()`` is
        True the ranking is computed in SQL by ``1 - (vector_plain
        <=> query)`` ordered by distance with ``LIMIT``. When
        False, the method paginates ``list_embeddings_page`` and
        computes ``cosine_similarity`` in Python — identical
        ranking, slower per-row cost.

        RLS: ``assume_app_role`` + ``set_current_wax_id`` scope
        every row the query reads to the calling student. The
        pgvector path reads only the plaintext ``vector_plain``
        column (no decryption needed); the fallback path decrypts
        each ``vector_encrypted`` blob with the per-student key.
        """
        ...

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

    def delete_search_tokens(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> int:
        """Drop all search-token rows for one object.

        Used by invalidation and deletion propagation. Returns the number
        of token rows dropped. Tokens are an index — removing them changes
        findability, not the underlying record.
        """
        ...

    def delete_embeddings(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> int:
        """Drop all embedding rows for one object.

        Used by invalidation and deletion propagation. Returns the number
        of embedding rows dropped. Embeddings are an index — removing them
        changes findability by meaning, not the underlying record.
        """
        ...

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

    def invalidate_notebook_entry(
        self, wax_id: WaxId, entry_id: UUID, *, now: datetime, reason: str
    ) -> NotebookEntry:
        """Mark a notebook entry as no longer trustworthy.

        Distinct from supersession: nothing replaces it. The entry stays
        for audit; ordinary retrieval should exclude it. The caller is
        responsible for also dropping the entry from search tokens and
        embeddings (see ``delete_search_tokens`` and ``delete_embeddings``).
        """
        ...

    # --- summaries --------------------------------------------------------
    def add_summary(self, summary: Summary) -> Summary: ...

    def get_summary(self, wax_id: WaxId, summary_id: UUID) -> Summary | None: ...

    def list_summaries(
        self, wax_id: WaxId, *, include_superseded: bool = False, limit: int = 50
    ) -> tuple[Summary, ...]: ...

    def supersede_summary(
        self, wax_id: WaxId, summary_id: UUID, now: datetime
    ) -> Summary: ...

    def invalidate_summary(
        self, wax_id: WaxId, summary_id: UUID, *, now: datetime, reason: str
    ) -> Summary:
        """Mark a summary as no longer trustworthy.

        Distinct from supersession: nothing replaces it. The summary stays
        for audit; ordinary retrieval should exclude it. Caller must also
        drop the summary from search tokens and embeddings.
        """
        ...

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

    def invalidate_node(
        self, wax_id: WaxId, node_id: UUID, *, now: datetime, reason: str
    ) -> KnowledgeNode:
        """Mark a knowledge node as no longer trustworthy.

        Distinct from supersession: nothing replaces it. The node stays
        for audit; ordinary retrieval should exclude it. Caller must also
        drop the node from search tokens and embeddings.
        """
        ...

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

    def get_outbound_delivery(
        self, wax_id: WaxId, delivery_id: UUID
    ) -> OutboundDelivery | None:
        """Fetch a single outbound delivery by id (read-only)."""
        ...

    def list_due_outbound_deliveries(
        self, wax_id: WaxId | None, now: datetime, limit: int
    ) -> tuple[OutboundDelivery, ...]: ...

    def claim_outbound_delivery(
        self, wax_id: WaxId, delivery_id: UUID, now: datetime
    ) -> OutboundDelivery | None:
        """Atomically claim a pending outbound for send (pending → in_progress).

        Returns the claimed row, or None if another worker already claimed or
        the row is already delivered/failed.
        """
        ...

    def try_acquire_turn_lock(self, wax_id: WaxId, student_entry_id: UUID) -> bool:
        """Cross-replica lock for teaching one student history entry.

        Postgres uses session advisory locks; memory uses an in-process set.
        """
        ...

    def release_turn_lock(self, wax_id: WaxId, student_entry_id: UUID) -> None:
        """Release a lock acquired by try_acquire_turn_lock."""
        ...

    def mark_outbound_delivered(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        provider_message_id: str | None = None,
    ) -> bool:
        """Atomically mark a delivery delivered.

        Returns ``True`` iff this call transitioned the row to delivered
        (``delivered_at`` was ``NULL``); ``False`` if the row was already
        delivered. The CAS guard ``delivered_at IS NULL`` makes the live
        progressive-delivery path and the durable outbox worker idempotent
        w.r.t. each other — fixes the double-message race where the outbox
        re-sent the full tutor text after progressive delivery had already
        pushed chunks to the student.
        """
        ...

    def mark_outbound_failed(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        """Mark a delivery failed. When ``retry_at`` is not None the row
        reverts to ``pending`` for retry and ``delivered_at`` is cleared so
        the row is internally consistent (the live progressive path may have
        optimistically marked delivered before progressive.finish raised).
        """
        ...

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

    # --- deletion requests (Blueprint Part IV §16-17) -------------------------------
    # Deletion is *not* supersession. It routes through a separate workflow
    # that respects retention policy and propagates through every dependent
    # representation. These methods let the workflow pick up requests, mark
    # progress, and record the propagation log. Retention policy is enforced
    # downstream — the storage layer only persists the request and the log
    # of what was propagated.
    def create_deletion_request(self, request: DeletionRequest) -> DeletionRequest: ...

    def get_deletion_request(
        self, wax_id: WaxId, request_id: UUID
    ) -> DeletionRequest | None: ...

    def list_deletion_requests(
        self, wax_id: WaxId, *, status: str | None = None, limit: int = 50
    ) -> tuple[DeletionRequest, ...]: ...

    def list_wax_ids_with_pending_deletion_requests(
        self, limit: int
    ) -> tuple[WaxId, ...]:
        """List every wax_id that has at least one PENDING deletion request.

        Used by the worker to drive the deletion workflow forward —
        for each wax_id returned, the worker then calls
        ``list_deletion_requests(wax_id, status='pending')`` and
        executes each via ``DeletionService``. The two-step API keeps
        the storage layer's surface small while still letting the
        worker find pending work without scanning all students.
        """
        ...

    def update_deletion_request(self, request: DeletionRequest) -> DeletionRequest:
        """Atomically update the status, completed_at, and propagation_log
        of a deletion request. Caller must supply the full new state;
        optimistic concurrency is the caller's responsibility (the storage
        layer writes what it is given)."""
        ...

    def claim_deletion_request(
        self,
        wax_id: WaxId,
        request_id: UUID,
        *,
        now: datetime,
        propagation_log: dict[str, Any],
    ) -> DeletionRequest | None:
        """Atomically transition a deletion request from PENDING to
        IN_PROGRESS, but only if it is currently PENDING. Returns the
        claimed request (with status=IN_PROGRESS) or None if another
        worker already claimed it or it has moved to a terminal state.

        This is the concurrency-safe claim primitive for the deletion
        workflow. The claim must be done *inside* the propagation
        transaction so that a failure rolls back the claim too —
        otherwise the request would be stuck in IN_PROGRESS forever
        if propagation raised after the claim committed. The
        ``DeletionService`` calls this inside its transaction.
        """
        ...

    # --- maintenance ----------------------------------------------------------------
    def list_orphan_student_ids(self, older_than: datetime) -> tuple[WaxId, ...]:
        """Students older than ``older_than`` with no channel identity and
        no data of any kind — leftovers from failed provisioning retries.
        """
        ...

    def delete_student(self, wax_id: WaxId) -> bool:
        """Hard-delete one student row. Returns True if a row was removed.

        Only safe for orphans from ``list_orphan_student_ids``: real
        students are protected by foreign keys from their data tables.
        """
        ...

    # --- learning states (Founder's vision §6) -------------------------------------
    # The third layer alongside history_entries (ground truth) and
    # notebook_entries (durable understanding): the current state of
    # the student's work on one subject — current topic, current stage,
    # demonstrated vs. developing vs. not-assessed, the teaching
    # observation, and the next sensible step. Superseded (never
    # edited) every time the student advances.
    def get_learning_state(
        self, wax_id: WaxId, subject: str | None = None
    ) -> LearningState | None:
        """Return the active learning state for ``subject`` (or the
        most recently active subject if ``subject is None``).

        When ``subject`` is provided, the lookup uses the blind HMAC
        index ``subject_hash`` (per-student derived key) — the database
        never sees the plaintext subject. RLS scopes by wax_id via
        ``assume_app_role`` + ``set_current_wax_id``.

        Founder's vision §6: a student who says "wait, why did we use
        4 there?" should retrieve the current learning state and
        continue without restarting the lesson. This method is the
        storage primitive CI uses to satisfy that.
        """
        ...

    def set_learning_state(
        self,
        wax_id: WaxId,
        subject: str,
        payload: dict[str, Any],
        *,
        now: datetime,
        paused: bool = False,
    ) -> LearningState:
        """Persist a new active learning state for ``subject``.

        Atomically supersedes any pre-existing active state for the
        same subject: the old row is set to status='superseded' with
        ``superseded_by`` pointing at the new row, and the new row is
        inserted with status='active'. The partial unique index
        ``learning_states_active_per_subject_idx`` (migration 018)
        guarantees at most one active state per (wax_id, subject) at
        any time.

        ``paused=True`` sets ``paused_at = now`` — the Founder's
        vision acknowledges a student may step away mid-lesson; CI can
        surface "we paused on quadratic equations yesterday" without
        losing the active state.

        All queries are parameterised; the GUC ``app.current_wax_id``
        is set transaction-locally via ``set_current_wax_id``.
        """
        ...

    def list_active_learning_states(
        self, wax_id: WaxId
    ) -> tuple[LearningState, ...]:
        """Return every active learning state for ``wax_id``, most
        recently active first.

        Used by CI to assemble the "what is the student working on"
        context across every subject. ``learning_states_recent_activity_idx``
        (migration 018) serves the order.

        Note: the plaintext subject is not recoverable from a row
        alone (only its blind HMAC hash). Callers that need the
        plaintext subject must know it from the call site.
        """
        ...

    # --- student calendar (Founder's vision §7) -------------------------------------
    # Exam dates, deadlines, school sessions, and other time-sensitive
    # events. ``event_date`` is plaintext so range queries run
    # server-side; ``notes`` is AES-GCM ciphertext at rest. The sweep
    # method ``expire_past_deadlines`` marks past deadlines without
    # deleting them — temporal truth is a first-class responsibility.
    def add_calendar_event(self, event: CalendarEvent) -> CalendarEvent:
        """Persist one calendar event.

        The event_date is stored as TIMESTAMPTZ (UTC). The student's
        local timezone (``students.timezone``, set via
        ``set_student_timezone``) is applied at presentation time,
        not at storage time — the database always works in UTC.

        ``notes`` is AES-GCM ciphertext, per-student derived key,
        purpose ``"student_calendar.notes"`` (migration 021 column
        comment).
        """
        ...

    def get_student_calendar(
        self,
        wax_id: WaxId,
        *,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        include_expired: bool = False,
    ) -> tuple[CalendarEvent, ...]:
        """Return calendar events for ``wax_id`` optionally filtered
        by date range.

        ``from_date`` and ``to_date`` form a half-open interval
        ``[from_date, to_date)`` on ``event_date``. When both are
        ``None``, every event for the student is returned. Expired
        deadlines (already swept by ``expire_past_deadlines``) are
        excluded by default; pass ``include_expired=True`` to include
        them (audit / "what deadlines already passed?" queries).

        Uses ``student_calendar_date_range_idx`` (migration 021) for
        an index-only range scan. RLS scopes by wax_id.
        """
        ...

    def expire_past_deadlines(self, now: datetime) -> int:
        """Sweep method: mark every past deadline as expired.

        Sets ``expired_at = now`` on every row where
        ``event_kind = 'deadline'`` AND ``event_date < now`` AND
        ``expired_at IS NULL``. Returns the number of rows newly
        marked expired. The scheduler calls this periodically so CI
        sees a consistent view: a deadline either has not yet arrived
        or it has been swept.

        Founder's vision §7-8: temporal truth is a first-class
        responsibility. A deadline that has passed must not appear
        as "upcoming" simply because the notebook said so yesterday.

        Cross-tenant: the UPDATE has no wax_id filter, so it must run
        as a role that can see every student's deadlines. The
        implementation resets to the bootstrap role for this one
        sweep so RLS does not blind it — same pattern as
        ``waxprep_students_with_due_jobs`` and
        ``waxprep_wax_ids_with_pending_deletions`` (SECURITY DEFINER
        helpers in migrations 005, 007, 014).
        """
        ...


__all__ = [
    "HistoryCursor",
    "ScoredMatch",
    "Storage",
    "TokenMatch",
    "TokenStats",
]
