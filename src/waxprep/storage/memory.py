"""In-memory storage for unit tests (not production)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactContent,
    ArtifactReference,
    ArtifactVersion,
    Attachment,
    AuthorKind,
    ChannelIdentity,
    DeletionRequest,
    DeletionRequestStatus,
    DerivationJob,
    EmbeddingRecord,
    Event,
    HistoryEntry,
    InteractiveOffer,
    InteractiveOfferStatus,
    InvestigationRecord,
    JobStatus,
    Notebook,
    NotebookEntry,
    NotebookEntryStatus,
    ObjectType,
    OutboundDelivery,
    OutboundStatus,
    ProvenanceLink,
    Student,
    Summary,
    SummaryStatus,
    Workspace,
    new_id,
)
from waxprep.knowledge.models import KnowledgeEdge, KnowledgeNode, KnowledgeNodeStatus
from waxprep.storage.protocol import TokenMatch, TokenStats

Key = tuple[WaxId, UUID]


class InMemoryStorage:
    """Deterministic in-process implementation of the Storage contract."""

    @contextlib.contextmanager
    def transaction(self) -> Iterator[InMemoryStorage]:
        """In-memory atomic transaction via snapshot/restore.

        On entry, take a deep copy of all internal state. On exception,
        restore from the snapshot — no partial state. On success,
        discard the snapshot.
        """
        import copy

        snapshot = copy.deepcopy(self.__dict__)
        try:
            yield self
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(snapshot)
            raise

    def __init__(self) -> None:
        self._students: dict[WaxId, Student] = {}
        self._channels: dict[tuple[str, str], ChannelIdentity] = {}
        self._history: dict[Key, HistoryEntry] = {}
        self._history_by_channel_ref: dict[tuple[WaxId, str, str], HistoryEntry] = {}
        self._history_seq: dict[Key, int] = {}
        self._attachments: dict[Key, Attachment] = {}
        self._tokens: dict[WaxId, list[TokenMatch]] = {}
        self._embeddings: dict[
            tuple[WaxId, ObjectType, UUID, str], EmbeddingRecord
        ] = {}
        self._notebooks: dict[WaxId, Notebook] = {}
        self._entries: dict[Key, NotebookEntry] = {}
        self._summaries: dict[Key, Summary] = {}
        self._provenance: list[ProvenanceLink] = []
        self._nodes: dict[Key, KnowledgeNode] = {}
        self._edges: dict[Key, KnowledgeEdge] = {}
        self._workspaces: dict[WaxId, Workspace] = {}
        self._artifacts: dict[Key, Artifact] = {}
        self._artifact_versions: dict[Key, ArtifactVersion] = {}
        self._artifact_references: dict[Key, list[ArtifactReference]] = {}
        self._artifact_content: dict[Key, ArtifactContent] = {}
        self._jobs: dict[Key, DerivationJob] = {}
        self._investigations: list[InvestigationRecord] = []
        self._events: dict[WaxId, list[Event]] = {}
        self._outbound: dict[Key, OutboundDelivery] = {}
        self._interactive_offers: dict[Key, InteractiveOffer] = {}
        # Blueprint Part IV §16-17: deletion requests are auditable and
        # propagated separately from supersession. The in-memory store
        # holds the request rows; the propagation log is a JSON dict on
        # each request.
        self._deletion_requests: dict[Key, DeletionRequest] = {}

    # --- identity -----------------------------------------------------
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

    def list_channel_identities_for_wax(
        self, wax_id: WaxId
    ) -> tuple[ChannelIdentity, ...]:
        return tuple(c for c in self._channels.values() if c.wax_id == wax_id)

    def list_outbound_deliveries_for_wax(
        self, wax_id: WaxId
    ) -> tuple[OutboundDelivery, ...]:
        return tuple(d for d in self._outbound.values() if d.wax_id == wax_id)

    # --- unified history ------------------------------------------------
    def append_history_entry(self, entry: HistoryEntry) -> HistoryEntry:
        if entry.wax_id not in self._students:
            raise ValueError("unknown student")
        if entry.channel is not None and entry.channel_ref is not None:
            for existing in self._history.values():
                if (
                    existing.wax_id == entry.wax_id
                    and existing.channel == entry.channel
                    and existing.channel_ref == entry.channel_ref
                ):
                    return existing
        if (entry.wax_id, entry.id) in self._history:
            raise ValueError("history entry already exists")
        self._history[(entry.wax_id, entry.id)] = entry
        self._history_seq[(entry.wax_id, entry.id)] = len(self._history_seq)
        if entry.channel is not None and entry.channel_ref is not None:
            self._history_by_channel_ref[
                (entry.wax_id, entry.channel, entry.channel_ref)
            ] = entry
        return entry

    def append_history_entry_with_jobs(
        self,
        entry: HistoryEntry,
        token_hashes: tuple[bytes, ...],
        jobs: tuple[DerivationJob, ...],
    ) -> tuple[HistoryEntry, bool]:
        """In-memory atomic write (Part 8 Blocker C).

        The contract mirrors PostgresStorage: if anything fails, no state is
        mutated. The in-memory store has no real transactions, but the rule
        is what unit tests test.
        """
        if entry.wax_id not in self._students:
            raise ValueError("unknown student")
        # Detect duplicate delivery first; do not mutate if duplicate.
        if entry.channel is not None and entry.channel_ref is not None:
            for existing in self._history.values():
                if (
                    existing.wax_id == entry.wax_id
                    and existing.channel == entry.channel
                    and existing.channel_ref == entry.channel_ref
                ):
                    return existing, False
        if (entry.wax_id, entry.id) in self._history:
            raise ValueError("history entry already exists")
        # All pre-checks passed; mutate.
        self._history[(entry.wax_id, entry.id)] = entry
        self._history_seq[(entry.wax_id, entry.id)] = len(self._history_seq)
        if entry.channel is not None and entry.channel_ref is not None:
            self._history_by_channel_ref[
                (entry.wax_id, entry.channel, entry.channel_ref)
            ] = entry
        if token_hashes:
            self._put_search_tokens_locked(
                entry.wax_id,
                ObjectType.HISTORY,
                entry.id,
                entry.occurred_at,
                token_hashes,
            )
        for job in jobs:
            self._jobs[(job.wax_id, job.id)] = job
        return entry, True

    def get_history_entry(self, wax_id: WaxId, entry_id: UUID) -> HistoryEntry | None:
        return self._history.get((wax_id, entry_id))

    def _sorted_history(self, wax_id: WaxId) -> list[HistoryEntry]:
        rows = [e for (w, _), e in self._history.items() if w == wax_id]
        return sorted(
            rows, key=lambda e: (e.occurred_at, self._history_seq[(wax_id, e.id)])
        )

    def list_history(
        self,
        wax_id: WaxId,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> tuple[HistoryEntry, ...]:
        rows = [
            e
            for e in self._sorted_history(wax_id)
            if (since is None or e.occurred_at >= since)
            and (until is None or e.occurred_at < until)
        ]
        if newest_first:
            rows.reverse()
        return tuple(rows[:limit])

    def find_history_by_channel_ref(
        self, wax_id: WaxId, channel: str, channel_ref: str
    ) -> HistoryEntry | None:
        key = (wax_id, channel, channel_ref)
        return self._history_by_channel_ref.get(key)

    def list_history_around(
        self, wax_id: WaxId, entry_id: UUID, *, before: int, after: int
    ) -> tuple[HistoryEntry, ...]:
        rows = self._sorted_history(wax_id)
        for index, entry in enumerate(rows):
            if entry.id == entry_id:
                return tuple(rows[max(0, index - before) : index + after + 1])
        return ()

    def list_unsummarized_history(
        self, wax_id: WaxId, *, before: datetime, limit: int
    ) -> tuple[HistoryEntry, ...]:
        covered = {
            link.source_id
            for link in self._provenance
            if link.wax_id == wax_id
            and link.derived_type is ObjectType.SUMMARY
            and link.source_type is ObjectType.HISTORY
            and (wax_id, link.derived_id) in self._summaries
        }
        rows = [
            e
            for e in self._sorted_history(wax_id)
            if e.occurred_at < before and e.id not in covered
        ]
        return tuple(rows[:limit])

    # --- attachments -------------------------------------------------------
    def create_attachment(self, attachment: Attachment) -> Attachment:
        if (attachment.wax_id, attachment.history_entry_id) not in self._history:
            raise ValueError("unknown history entry for student")
        key = (attachment.wax_id, attachment.id)
        if key in self._attachments:
            raise ValueError("attachment already exists")
        self._attachments[key] = attachment
        return attachment

    def get_attachment(self, wax_id: WaxId, attachment_id: UUID) -> Attachment | None:
        return self._attachments.get((wax_id, attachment_id))

    def list_attachments_for_entry(
        self, wax_id: WaxId, entry_id: UUID
    ) -> tuple[Attachment, ...]:
        return tuple(
            a
            for (w, _), a in self._attachments.items()
            if w == wax_id and a.history_entry_id == entry_id
        )

    # --- search index -------------------------------------------------------
    def _put_search_tokens_locked(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        object_time: datetime,
        token_hashes: tuple[bytes, ...],
    ) -> None:
        """Write tokens without re-validating ownership (caller holds the lock)."""
        rows = self._tokens.setdefault(wax_id, [])
        rows[:] = [
            r
            for r in rows
            if not (r.object_type is object_type and r.object_id == object_id)
        ]
        for token_hash in dict.fromkeys(token_hashes):
            rows.append(TokenMatch(object_type, object_id, object_time, token_hash))

    def put_search_tokens(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        object_time: datetime,
        token_hashes: tuple[bytes, ...],
    ) -> None:
        self._put_search_tokens_locked(
            wax_id, object_type, object_id, object_time, token_hashes
        )

    def token_stats(
        self,
        wax_id: WaxId,
        token_hashes: tuple[bytes, ...],
        object_types: tuple[ObjectType, ...],
    ) -> TokenStats:
        rows = [
            r for r in self._tokens.get(wax_id, []) if r.object_type in object_types
        ]
        total = len({(r.object_type, r.object_id) for r in rows})
        wanted = set(token_hashes)
        frequency: dict[bytes, int] = {}
        for row in rows:
            if row.token_hash in wanted:
                frequency[row.token_hash] = frequency.get(row.token_hash, 0) + 1
        return TokenStats(total_objects=total, document_frequency=frequency)

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
        wanted = set(token_hashes)
        rows = [
            r
            for r in self._tokens.get(wax_id, [])
            if r.token_hash in wanted
            and r.object_type in object_types
            and (since is None or r.object_time >= since)
            and (until is None or r.object_time < until)
        ]
        # Deterministic order: object_time DESC, object_id, token_hash.
        rows.sort(
            key=lambda r: (r.object_time, r.object_id, r.token_hash),
            reverse=True,
        )
        return tuple(rows[:limit] if limit is not None else rows)

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
        wanted = set(token_hashes)
        rows = [
            r
            for r in self._tokens.get(wax_id, [])
            if r.token_hash in wanted
            and r.object_type in object_types
            and (since is None or r.object_time >= since)
            and (until is None or r.object_time < until)
        ]
        rows.sort(
            key=lambda r: (r.object_time, r.object_id, r.token_hash),
            reverse=True,
        )
        if cursor is not None:
            cur_time, cur_id, cur_hash = cursor
            rows = [
                r
                for r in rows
                if (r.object_time, r.object_id, r.token_hash)
                < (cur_time, cur_id, cur_hash)
            ]
        return tuple(rows[:page_size])

    def put_embedding(self, record: EmbeddingRecord) -> None:
        key = (record.wax_id, record.object_type, record.object_id, record.model)
        self._embeddings[key] = record

    def delete_search_tokens(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> int:
        """Drop all search-token rows for one object.

        Used by invalidation and deletion propagation. Returns the number
        of token rows dropped. Tokens are an index — removing them changes
        findability, not the underlying record.
        """
        rows = self._tokens.get(wax_id, [])
        kept: list[TokenMatch] = []
        dropped = 0
        for row in rows:
            if row.object_type is object_type and row.object_id == object_id:
                dropped += 1
                continue
            kept.append(row)
        if dropped:
            self._tokens[wax_id] = kept
        return dropped

    def delete_embeddings(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> int:
        """Drop all embedding rows for one object.

        Used by invalidation and deletion propagation. Returns the number
        of embedding rows dropped. Embeddings are an index — removing them
        changes findability by meaning, not the underlying record.
        """
        # Embeddings are keyed by (wax_id, object_type, object_id, model).
        # Drop every row for this (wax_id, object_type, object_id) regardless
        # of which model produced it — different models can have multiple
        # embedding rows for the same source object.
        keys_to_drop = [
            (w, ot, oid, model)
            for (w, ot, oid, model) in self._embeddings
            if w == wax_id and ot is object_type and oid == object_id
        ]
        for key in keys_to_drop:
            del self._embeddings[key]
        return len(keys_to_drop)

    def list_embeddings(
        self,
        wax_id: WaxId,
        object_types: tuple[ObjectType, ...],
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> tuple[EmbeddingRecord, ...]:
        rows = [
            r
            for (w, _, _, _), r in self._embeddings.items()
            if w == wax_id
            and r.object_type in object_types
            and (since is None or r.object_time >= since)
            and (until is None or r.object_time < until)
        ]
        rows.sort(key=lambda r: (r.object_time, r.object_id), reverse=True)
        return tuple(rows[:limit] if limit is not None else rows)

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
        rows = [
            r
            for (w, _, _, _), r in self._embeddings.items()
            if w == wax_id
            and r.object_type in object_types
            and (since is None or r.object_time >= since)
            and (until is None or r.object_time < until)
        ]
        rows.sort(key=lambda r: (r.object_time, r.object_id), reverse=True)
        if cursor is not None:
            cur_time, cur_id = cursor
            rows = [
                r for r in rows if (r.object_time, r.object_id) < (cur_time, cur_id)
            ]
        return tuple(rows[:page_size])

    # --- notebook -------------------------------------------------------------
    def get_or_create_notebook(self, wax_id: WaxId, now: datetime) -> Notebook:
        existing = self._notebooks.get(wax_id)
        if existing is not None:
            return existing
        if wax_id not in self._students:
            raise ValueError("unknown student")
        notebook = Notebook(
            id=new_id(), wax_id=wax_id, version=1, created_at=now, updated_at=now
        )
        self._notebooks[wax_id] = notebook
        return notebook

    def get_notebook(self, wax_id: WaxId) -> Notebook | None:
        return self._notebooks.get(wax_id)

    def add_notebook_entry(self, entry: NotebookEntry) -> NotebookEntry:
        notebook = self._notebooks.get(entry.wax_id)
        if notebook is None or notebook.id != entry.notebook_id:
            raise ValueError("unknown notebook for student")
        self._entries[(entry.wax_id, entry.id)] = entry
        return entry

    def get_notebook_entry(self, wax_id: WaxId, entry_id: UUID) -> NotebookEntry | None:
        return self._entries.get((wax_id, entry_id))

    def list_notebook_entries(
        self, wax_id: WaxId, notebook_id: UUID, *, include_superseded: bool = False
    ) -> tuple[NotebookEntry, ...]:
        rows = [
            e
            for (w, _), e in self._entries.items()
            if w == wax_id
            and e.notebook_id == notebook_id
            and (include_superseded or e.status is NotebookEntryStatus.ACTIVE)
        ]
        return tuple(sorted(rows, key=lambda e: e.created_at))

    def supersede_notebook_entry(
        self, wax_id: WaxId, old_id: UUID, new_id: UUID, now: datetime
    ) -> NotebookEntry:
        old = self._entries.get((wax_id, old_id))
        if old is None or (wax_id, new_id) not in self._entries:
            raise ValueError("unknown notebook entry")
        updated = replace(
            old,
            status=NotebookEntryStatus.SUPERSEDED,
            superseded_by_id=new_id,
            updated_at=now,
        )
        self._entries[(wax_id, old_id)] = updated
        return updated

    def invalidate_notebook_entry(
        self, wax_id: WaxId, entry_id: UUID, *, now: datetime, reason: str
    ) -> NotebookEntry:
        """Mark a notebook entry as no longer trustworthy (not superseded).

        Distinct from supersession: nothing replaces it. The entry stays
        for audit; ordinary retrieval should exclude it. Caller is
        responsible for also dropping the entry's search tokens and
        embeddings (see ``delete_search_tokens`` and ``delete_embeddings``).
        """
        old = self._entries.get((wax_id, entry_id))
        if old is None:
            raise ValueError("unknown notebook entry")
        # Preserve superseded_by_id if it was already superseded —
        # invalidation is a separate fact about the entry's trustworthiness,
        # not a replacement of its supersession relationship.
        updated = replace(
            old,
            status=NotebookEntryStatus.INVALIDATED,
            updated_at=now,
        )
        # Record an Event so the audit trail captures the invalidation
        # even after the entry row is later archived. The free-form
        # ``reason`` text is NOT stored in event metadata — it can be
        # student-derived and the operational-only contract forbids it.
        # The reason lives on the proposal/audit trail of the caller.
        self.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind="notebook_entry_invalidated",
                object_type=ObjectType.NOTEBOOK.value,
                object_id=entry_id,
                created_at=now,
                metadata={"status": NotebookEntryStatus.INVALIDATED.value},
            )
        )
        # ``reason`` is preserved on the caller's proposal record; we
        # drop it here to avoid putting potentially-student-derived text
        # into operational event metadata.
        _ = reason
        self._entries[(wax_id, entry_id)] = updated
        return updated

    # --- summaries ---------------------------------------------------------------
    def add_summary(self, summary: Summary) -> Summary:
        if summary.wax_id not in self._students:
            raise ValueError("unknown student")
        self._summaries[(summary.wax_id, summary.id)] = summary
        return summary

    def get_summary(self, wax_id: WaxId, summary_id: UUID) -> Summary | None:
        return self._summaries.get((wax_id, summary_id))

    def list_summaries(
        self, wax_id: WaxId, *, include_superseded: bool = False, limit: int = 50
    ) -> tuple[Summary, ...]:
        rows = [
            s
            for (w, _), s in self._summaries.items()
            if w == wax_id and (include_superseded or s.status is SummaryStatus.ACTIVE)
        ]
        rows.sort(key=lambda s: s.covers_until, reverse=True)
        return tuple(rows[:limit])

    def supersede_summary(
        self, wax_id: WaxId, summary_id: UUID, now: datetime
    ) -> Summary:
        del now
        old = self._summaries.get((wax_id, summary_id))
        if old is None:
            raise ValueError("unknown summary")
        updated = replace(old, status=SummaryStatus.SUPERSEDED)
        self._summaries[(wax_id, summary_id)] = updated
        return updated

    def invalidate_summary(
        self, wax_id: WaxId, summary_id: UUID, *, now: datetime, reason: str
    ) -> Summary:
        """Mark a summary as no longer trustworthy (not superseded).

        The free-form ``reason`` text is not stored in event metadata
        (it can be student-derived). It lives on the caller's proposal
        audit trail.
        """
        old = self._summaries.get((wax_id, summary_id))
        if old is None:
            raise ValueError("unknown summary")
        updated = replace(
            old,
            status=SummaryStatus.INVALIDATED,
        )
        self.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind="summary_invalidated",
                object_type=ObjectType.SUMMARY.value,
                object_id=summary_id,
                created_at=now,
                metadata={"status": SummaryStatus.INVALIDATED.value},
            )
        )
        _ = reason
        self._summaries[(wax_id, summary_id)] = updated
        return updated

    # --- provenance ----------------------------------------------------------------
    def add_provenance_links(self, links: tuple[ProvenanceLink, ...]) -> None:
        for link in links:
            if link.wax_id not in self._students:
                raise ValueError("unknown student")
            # Blocker F: polymorphic provenance links cannot be protected
            # by an ordinary FK; verify ownership at the service level.
            self._check_object_owner(link.wax_id, link.source_type, link.source_id)
            self._check_object_owner(link.wax_id, link.derived_type, link.derived_id)
        self._provenance.extend(links)

    def _check_object_owner(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> None:
        """Raise ValueError if the object exists but is owned by another student."""
        owner = self._object_owner(wax_id, object_type, object_id)
        if owner is None:
            return  # missing object — a separate error, not a cross-tenant ref
        if owner != wax_id:
            raise ValueError(
                f"cross-student reference: {object_type.value} {object_id} "
                f"is owned by another student"
            )

    def _object_owner(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> WaxId | None:
        if object_type is ObjectType.HISTORY:
            entry = self._history.get((wax_id, object_id))
            return entry.wax_id if entry else None
        if object_type is ObjectType.SUMMARY:
            s = self._summaries.get((wax_id, object_id))
            return s.wax_id if s else None
        if object_type is ObjectType.NOTEBOOK:
            n = self._entries.get((wax_id, object_id))
            return n.wax_id if n else None
        if object_type is ObjectType.KNOWLEDGE_NODE:
            n = self._nodes.get((wax_id, object_id))
            return n.wax_id if n else None
        if object_type is ObjectType.KNOWLEDGE_EDGE:
            e = self._edges.get((wax_id, object_id))
            return e.wax_id if e else None
        if object_type is ObjectType.ARTIFACT_VERSION:
            v = self._artifact_versions.get((wax_id, object_id))
            return v.wax_id if v else None
        if object_type is ObjectType.ARTIFACT_CONTENT:
            c = self._artifact_content.get((wax_id, object_id))
            return c.wax_id if c else None
        return None

    def list_provenance(
        self, wax_id: WaxId, derived_type: ObjectType, derived_id: UUID
    ) -> tuple[ProvenanceLink, ...]:
        return tuple(
            link
            for link in self._provenance
            if link.wax_id == wax_id
            and link.derived_type is derived_type
            and link.derived_id == derived_id
        )

    def list_derived_from(
        self, wax_id: WaxId, source_type: ObjectType, source_id: UUID
    ) -> tuple[ProvenanceLink, ...]:
        return tuple(
            link
            for link in self._provenance
            if link.wax_id == wax_id
            and link.source_type is source_type
            and link.source_id == source_id
        )

    # --- knowledge graph --------------------------------------------------------------
    def save_node(self, node: KnowledgeNode) -> KnowledgeNode:
        if node.wax_id not in self._students:
            raise ValueError("unknown student")
        self._nodes[(node.wax_id, node.id)] = node
        return node

    def get_node(self, wax_id: WaxId, node_id: UUID) -> KnowledgeNode | None:
        return self._nodes.get((wax_id, node_id))

    def add_edge(self, edge: KnowledgeEdge) -> KnowledgeEdge:
        if (edge.wax_id, edge.source_node_id) not in self._nodes:
            raise ValueError("unknown source node")
        if (edge.wax_id, edge.target_node_id) not in self._nodes:
            raise ValueError("unknown target node")
        self._edges[(edge.wax_id, edge.id)] = edge
        return edge

    def get_edge(self, wax_id: WaxId, edge_id: UUID) -> KnowledgeEdge | None:
        return self._edges.get((wax_id, edge_id))

    def list_edges(
        self, wax_id: WaxId, node_id: UUID, *, direction: str, limit: int
    ) -> tuple[KnowledgeEdge, ...]:
        rows: list[KnowledgeEdge] = []
        for (w, _), edge in self._edges.items():
            if w != wax_id:
                continue
            outgoing = edge.source_node_id == node_id
            incoming = edge.target_node_id == node_id
            if (direction in ("outgoing", "both") and outgoing) or (
                direction in ("incoming", "both") and incoming
            ):
                rows.append(edge)
        return tuple(rows[:limit])

    def invalidate_node(
        self, wax_id: WaxId, node_id: UUID, *, now: datetime, reason: str
    ) -> KnowledgeNode:
        """Mark a knowledge node as no longer trustworthy (not superseded).

        Distinct from supersession: nothing replaces it. The node stays
        for audit; ordinary retrieval should exclude it. Caller is
        responsible for also dropping the node's search tokens and
        embeddings. ``reason`` is not stored in event metadata — it
        lives on the caller's proposal audit trail.
        """
        old = self._nodes.get((wax_id, node_id))
        if old is None:
            raise ValueError("unknown knowledge node")
        updated = replace(
            old,
            status=KnowledgeNodeStatus.INVALIDATED,
            updated_at=now,
        )
        self.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind="knowledge_node_invalidated",
                object_type=ObjectType.KNOWLEDGE_NODE.value,
                object_id=node_id,
                created_at=now,
                metadata={"status": KnowledgeNodeStatus.INVALIDATED.value},
            )
        )
        _ = reason
        self._nodes[(wax_id, node_id)] = updated
        return updated

    # --- workspace and artifacts ----------------------------------------------------------
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
        rows = [
            a
            for (owner, _), a in self._artifacts.items()
            if owner == wax_id and a.workspace_id == workspace_id
        ]
        return tuple(sorted(rows, key=lambda a: a.created_at))

    def create_artifact_version(self, version: ArtifactVersion) -> ArtifactVersion:
        if (version.wax_id, version.artifact_id) not in self._artifacts:
            raise ValueError("unknown artifact for student")
        if version.version_number < 1:
            raise ValueError("version_number must be >= 1")
        existing = self.list_artifact_versions(version.wax_id, version.artifact_id)
        if any(v.version_number == version.version_number for v in existing):
            raise ValueError("artifact version number already exists")
        key = (version.wax_id, version.id)
        if key in self._artifact_versions:
            raise ValueError("artifact version already exists")
        self._artifact_versions[key] = version
        return version

    def get_artifact_version(
        self, wax_id: WaxId, artifact_id: UUID, version_id: UUID
    ) -> ArtifactVersion | None:
        version = self._artifact_versions.get((wax_id, version_id))
        if version is None or version.artifact_id != artifact_id:
            return None
        return version

    def get_artifact_version_by_id(
        self, wax_id: WaxId, version_id: UUID
    ) -> ArtifactVersion | None:
        return self._artifact_versions.get((wax_id, version_id))

    def list_artifact_versions(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactVersion, ...]:
        rows = [
            v
            for (owner, _), v in self._artifact_versions.items()
            if owner == wax_id and v.artifact_id == artifact_id
        ]
        return tuple(sorted(rows, key=lambda v: v.version_number))

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
        if self.get_artifact_version(wax_id, artifact_id, version_id) is None:
            raise ValueError("unknown artifact version")
        updated = replace(
            artifact, current_version_id=version_id, updated_at=updated_at
        )
        self._artifacts[(wax_id, artifact_id)] = updated
        return updated

    def create_artifact_reference(
        self, reference: ArtifactReference
    ) -> ArtifactReference:
        if (reference.wax_id, reference.artifact_id) not in self._artifacts:
            raise ValueError("unknown artifact for student")
        key = (reference.wax_id, reference.artifact_id)
        references = self._artifact_references.setdefault(key, [])
        if any(
            r.reference_type == reference.reference_type
            and r.reference_id == reference.reference_id
            for r in references
        ):
            raise ValueError("artifact reference already exists")
        references.append(reference)
        return reference

    def list_artifact_references(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactReference, ...]:
        return tuple(self._artifact_references.get((wax_id, artifact_id), []))

    def add_artifact_content(self, content: ArtifactContent) -> ArtifactContent:
        if (content.wax_id, content.artifact_version_id) not in self._artifact_versions:
            raise ValueError("unknown artifact version for student")
        self._artifact_content[(content.wax_id, content.id)] = content
        return content

    def get_artifact_content(
        self, wax_id: WaxId, content_id: UUID
    ) -> ArtifactContent | None:
        return self._artifact_content.get((wax_id, content_id))

    def list_artifact_content(
        self, wax_id: WaxId, artifact_version_id: UUID
    ) -> tuple[ArtifactContent, ...]:
        rows = [
            c
            for (w, _), c in self._artifact_content.items()
            if w == wax_id and c.artifact_version_id == artifact_version_id
        ]
        return tuple(sorted(rows, key=lambda c: c.created_at))

    # --- background work -----------------------------------------------------------------------
    def enqueue_job(self, job: DerivationJob) -> DerivationJob:
        if job.wax_id not in self._students:
            raise ValueError("unknown student")
        self._jobs[(job.wax_id, job.id)] = job
        return job

    def list_wax_ids_with_due_jobs(
        self, now: datetime, limit: int
    ) -> tuple[WaxId, ...]:
        due = {
            j.wax_id
            for j in self._jobs.values()
            if (j.status is JobStatus.PENDING and j.run_after <= now)
            or (
                j.status is JobStatus.RUNNING
                and j.lease_until is not None
                and j.lease_until <= now
            )
        }
        return tuple(sorted(due, key=str)[:limit])

    def find_tutor_reply_to(
        self, wax_id: WaxId, student_entry_id: UUID
    ) -> HistoryEntry | None:
        """Return the tutor history entry that replies to a student entry, if any."""
        for (w, _), entry in self._history.items():
            if (
                w == wax_id
                and entry.author_kind is AuthorKind.TUTOR
                and entry.reply_to_id == student_entry_id
            ):
                return entry
        return None

    def claim_due_jobs(
        self, wax_id: WaxId, now: datetime, limit: int = 10, *, lease_seconds: int = 300
    ) -> tuple[DerivationJob, ...]:
        """Atomically claim due jobs (pending or expired leases)."""
        from datetime import timedelta

        claimed: list[DerivationJob] = []
        lease_until = now + timedelta(seconds=lease_seconds)
        for (w, jid), j in list(self._jobs.items()):
            if w != wax_id:
                continue
            due = (j.status is JobStatus.PENDING and j.run_after <= now) or (
                j.status is JobStatus.RUNNING
                and j.lease_until is not None
                and j.lease_until <= now
            )
            if not due:
                continue
            updated = DerivationJob(
                id=j.id,
                wax_id=j.wax_id,
                kind=j.kind,
                object_type=j.object_type,
                object_id=j.object_id,
                status=JobStatus.RUNNING,
                attempts=j.attempts + 1,
                last_error=j.last_error,
                run_after=j.run_after,
                created_at=j.created_at,
                updated_at=now,
                lease_until=lease_until,
            )
            self._jobs[(w, jid)] = updated
            claimed.append(updated)
            if len(claimed) >= limit:
                break
        return tuple(claimed)

    def release_job_capability_skip(
        self, wax_id: WaxId, job_id: UUID, now: datetime, retry_at: datetime
    ) -> None:
        """Return a job to pending without counting a failed attempt."""
        job = self._jobs[(wax_id, job_id)]
        self._jobs[(wax_id, job_id)] = replace(
            job,
            status=JobStatus.PENDING,
            attempts=max(0, job.attempts - 1),
            last_error="capability_unavailable",
            run_after=retry_at,
            updated_at=now,
            lease_until=None,
        )

    def mark_job_done(self, wax_id: WaxId, job_id: UUID, now: datetime) -> None:
        job = self._jobs[(wax_id, job_id)]
        # Success does not increment attempts; the attempt was counted
        # at claim time. Clear the lease so the row is no longer
        # considered for re-claim by another worker.
        self._jobs[(wax_id, job_id)] = replace(
            job, status=JobStatus.DONE, lease_until=None, updated_at=now
        )

    def mark_job_failed(
        self,
        wax_id: WaxId,
        job_id: UUID,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        job = self._jobs[(wax_id, job_id)]
        # Blocker (spec 8.2-#10): the attempt was already counted at
        # claim time. Failure records the result of the same attempt;
        # it must NOT increment attempts again.
        self._jobs[(wax_id, job_id)] = replace(
            job,
            status=JobStatus.PENDING if retry_at is not None else JobStatus.FAILED,
            last_error=error,
            run_after=retry_at or job.run_after,
            lease_until=None,
            updated_at=now,
        )

    def list_jobs(self, wax_id: WaxId) -> tuple[DerivationJob, ...]:
        """Test helper: every job for one student."""
        rows = [j for (w, _), j in self._jobs.items() if w == wax_id]
        return tuple(sorted(rows, key=lambda j: j.created_at))

    # --- outbound delivery (outbox) ----------------------------------------------
    def create_outbound_delivery(self, delivery: OutboundDelivery) -> OutboundDelivery:
        key = (delivery.wax_id, delivery.id)
        if key in self._outbound:
            # Idempotent: return the existing one.
            return self._outbound[key]
        # Check for an existing delivery for the same (tutor, channel).
        for existing in self._outbound.values():
            if (
                existing.wax_id == delivery.wax_id
                and existing.tutor_entry_id == delivery.tutor_entry_id
                and existing.channel == delivery.channel
            ):
                return existing
        self._outbound[key] = delivery
        return delivery

    def get_outbound_delivery_for_tutor(
        self, wax_id: WaxId, tutor_entry_id: UUID, channel: str | None
    ) -> OutboundDelivery | None:
        for d in self._outbound.values():
            if (
                d.wax_id == wax_id
                and d.tutor_entry_id == tutor_entry_id
                and d.channel == channel
            ):
                return d
        return None

    def list_due_outbound_deliveries(
        self, wax_id: WaxId | None, now: datetime, limit: int
    ) -> tuple[OutboundDelivery, ...]:
        due = [
            d
            for d in self._outbound.values()
            if (wax_id is None or d.wax_id == wax_id)
            and d.status in (OutboundStatus.PENDING, OutboundStatus.IN_PROGRESS)
            and d.run_after <= now
        ]
        due.sort(key=lambda d: d.run_after)
        return tuple(due[:limit])

    def mark_outbound_delivered(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        provider_message_id: str | None = None,
    ) -> None:
        key = (wax_id, delivery_id)
        old = self._outbound[key]
        self._outbound[key] = replace(
            old,
            status=OutboundStatus.DELIVERED,
            provider_message_id=provider_message_id or old.provider_message_id,
            delivered_at=now,
            updated_at=now,
        )

    def mark_outbound_failed(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        key = (wax_id, delivery_id)
        old = self._outbound[key]
        self._outbound[key] = replace(
            old,
            status=OutboundStatus.PENDING
            if retry_at is not None
            else OutboundStatus.FAILED,
            attempts=old.attempts + 1,
            last_error=error,
            run_after=retry_at or old.run_after,
            updated_at=now,
        )

    # --- interactive offers ---------------------------------------------------------
    def create_interactive_offer(self, offer: InteractiveOffer) -> InteractiveOffer:
        key = (offer.wax_id, offer.id)
        if key in self._interactive_offers:
            raise ValueError("interactive offer already exists")
        self._interactive_offers[key] = offer
        return offer

    def find_interactive_offer_by_button_id(
        self, wax_id: WaxId, button_id: str
    ) -> InteractiveOffer | None:
        for (wid, _), offer in self._interactive_offers.items():
            if wid != wax_id:
                continue
            for btn in offer.buttons:
                if btn.get("id") == button_id:
                    return offer
        return None

    def consume_interactive_offer(
        self,
        wax_id: WaxId,
        offer_id: UUID,
        button_id: str,
        now: datetime,
    ) -> InteractiveOffer | None:
        key = (wax_id, offer_id)
        old = self._interactive_offers.get(key)
        if old is None or old.status != InteractiveOfferStatus.OPEN:
            return None
        if old.expires_at is not None and old.expires_at <= now:
            self._interactive_offers[key] = replace(
                old,
                status=InteractiveOfferStatus.EXPIRED,
                updated_at=now,
            )
            return None
        updated = replace(
            old,
            status=InteractiveOfferStatus.CONSUMED,
            consumed_button_id=button_id,
            consumed_at=now,
            updated_at=now,
        )
        self._interactive_offers[key] = updated
        return updated

    # --- audit --------------------------------------------------------------------------------------
    def record_investigation(self, record: InvestigationRecord) -> InvestigationRecord:
        self._investigations.append(record)
        return record

    def append_event(self, event: Event) -> Event:
        from waxprep.security.event_metadata import validate_event_metadata

        if event.wax_id is None:
            return event
        validate_event_metadata(event.metadata)
        self._events.setdefault(event.wax_id, []).append(event)
        return event

    def list_events(self, wax_id: WaxId) -> tuple[Event, ...]:
        return tuple(self._events.get(wax_id, []))

    # --- deletion requests (Blueprint Part IV §16-17) ---------------------------------------------
    def create_deletion_request(self, request: DeletionRequest) -> DeletionRequest:
        key = (request.wax_id, request.id)
        if key in self._deletion_requests:
            raise ValueError("deletion request already exists")
        if request.wax_id not in self._students:
            raise ValueError("unknown student")
        self._deletion_requests[key] = request
        # Record an event so the audit trail has the request even after
        # the request row is removed by retention. The sensitive
        # ``scope`` and ``reason`` text is NOT stored in event metadata
        # — it lives on the deletion_requests row itself, encrypted at
        # rest by the storage layer where applicable.
        self.append_event(
            Event(
                id=new_id(),
                wax_id=request.wax_id,
                kind="deletion_request_created",
                object_type=request.object_type.value,
                object_id=request.object_id,
                created_at=request.requested_at,
                metadata={
                    "status": request.status.value,
                    "request_id": str(request.id),
                },
            )
        )
        return request

    def get_deletion_request(
        self, wax_id: WaxId, request_id: UUID
    ) -> DeletionRequest | None:
        return self._deletion_requests.get((wax_id, request_id))

    def list_deletion_requests(
        self, wax_id: WaxId, *, status: str | None = None, limit: int = 50
    ) -> tuple[DeletionRequest, ...]:
        rows = [
            r
            for (w, _), r in self._deletion_requests.items()
            if w == wax_id and (status is None or r.status.value == status)
        ]
        rows.sort(key=lambda r: r.requested_at, reverse=True)
        return tuple(rows[:limit])

    def list_wax_ids_with_pending_deletion_requests(
        self, limit: int
    ) -> tuple[WaxId, ...]:
        """Return up to ``limit`` wax_ids that have at least one PENDING
        deletion request. Order is insertion-order for determinism."""
        out: list[WaxId] = []
        seen: set[WaxId] = set()
        for (w, _), r in self._deletion_requests.items():
            if w in seen:
                continue
            if r.status is DeletionRequestStatus.PENDING:
                out.append(w)
                seen.add(w)
            if len(out) >= limit:
                break
        return tuple(out)

    def update_deletion_request(self, request: DeletionRequest) -> DeletionRequest:
        key = (request.wax_id, request.id)
        if key not in self._deletion_requests:
            raise ValueError("unknown deletion request")
        # Record the transition as an event so the audit trail captures
        # the workflow's progress without re-reading the request row.
        # Sensitive text stays on the request row.
        self.append_event(
            Event(
                id=new_id(),
                wax_id=request.wax_id,
                kind="deletion_request_updated",
                object_type=request.object_type.value,
                object_id=request.object_id,
                created_at=request.completed_at or request.requested_at,
                metadata={
                    "status": request.status.value,
                    "request_id": str(request.id),
                },
            )
        )
        self._deletion_requests[key] = request
        return request

    def claim_deletion_request(
        self,
        wax_id: WaxId,
        request_id: UUID,
        *,
        now: datetime,
        propagation_log: dict[str, Any],
    ) -> DeletionRequest | None:
        """Atomically transition PENDING → IN_PROGRESS. Returns the
        claimed request or None if another worker already claimed it.

        In the in-memory storage this is a read-check-write because the
        storage's transaction context serialises access within one
        process. For Postgres the same logic is a conditional
        UPDATE ... WHERE status='pending' RETURNING *.
        """
        del now  # timestamp is in the propagation_log; the row's
        # requested_at is preserved.
        key = (wax_id, request_id)
        current = self._deletion_requests.get(key)
        if current is None:
            return None
        if current.status is not DeletionRequestStatus.PENDING:
            # Already claimed by another worker, or already terminal.
            return None
        claimed = replace(
            current,
            status=DeletionRequestStatus.IN_PROGRESS,
            propagation_log=dict(propagation_log),
        )
        self._deletion_requests[key] = claimed
        return claimed


__all__ = ["InMemoryStorage"]
