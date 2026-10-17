"""Makes saved objects findable without ever blocking or losing them.

The Indexer is **purely a planner**: it never touches the database itself.
Producing token hashes and the list of derivation jobs to enqueue is a pure
function of the input text and the configured capabilities. Persistence
of those hashes and jobs is the caller's responsibility — usually the
atomic ``Storage.append_history_entry_with_jobs`` operation so that a
history entry, its token index, and its embedding job all land in one
transaction (Part 8 Blocker C).

Embedding jobs are planned only when an embedding provider is configured
(Part 8 Blocker B). When an embedder is added later, call
``backfill_embedding_jobs`` to plan and enqueue embedding work for objects
that were saved before the embedder existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import DerivationJob, JobStatus, ObjectType, new_id
from waxprep.index.tokens import TokenIndex
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)

JOB_INDEX_TOKENS = "index_tokens"
JOB_EMBED = "embed"
JOB_UNDERSTAND_ARTIFACT = "understand_artifact"
JOB_PROCESS_TURN = "process_turn"


@dataclass(frozen=True, slots=True)
class IndexPlan:
    """Result of planning index work for one object.

    Attributes:
        token_hashes: HMAC hashes of normalized words, written to the blind
            word index. Empty when the source text is blank.
        jobs: Derivation jobs to enqueue (typically ``JOB_EMBED`` and/or
            ``JOB_UNDERSTAND_ARTIFACT``). Empty when no relevant capability
            is configured or the source has no searchable text.
    """

    token_hashes: tuple[bytes, ...]
    jobs: tuple[DerivationJob, ...]


class Indexer:
    """Plans index work; never raises into the caller.

    The object being indexed is already saved by the time ``plan`` is
    called, and indexing can always be retried by re-planning and
    re-enqueuing. ``enqueue`` is provided for the historical non-atomic
    call path (kept for backfill and operational recovery only).
    """

    def __init__(
        self,
        storage: Storage,
        token_index: TokenIndex,
        clock: Clock,
        *,
        has_embedder: bool = False,
        has_artifact_processor: bool = False,
    ) -> None:
        self._storage = storage
        self._token_index = token_index
        self._clock = clock
        self._has_embedder = has_embedder
        self._has_artifact_processor = has_artifact_processor

    def plan(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        text: str | None,
        object_time: datetime,
    ) -> IndexPlan:
        """Pure: produce token hashes and the jobs to enqueue for this object."""
        now = self._clock.now()
        token_hashes: tuple[bytes, ...] = ()
        jobs: list[DerivationJob] = []
        if text and text.strip():
            token_hashes = self._token_index.hashes(wax_id, text)
            if self._has_embedder:
                jobs.append(
                    self._new_job(wax_id, JOB_EMBED, object_type, object_id, now)
                )
        return IndexPlan(token_hashes=token_hashes, jobs=tuple(jobs))

    # Backwards-compatible single-object write path. New code should call
    # ``plan`` + ``Storage.append_history_entry_with_jobs`` so the entry,
    # its token hashes, and its jobs commit together (Blocker C).
    def index(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        text: str,
        object_time: datetime,
    ) -> None:
        plan = self.plan(wax_id, object_type, object_id, text, object_time)
        try:
            if plan.token_hashes:
                self._storage.put_search_tokens(
                    wax_id,
                    object_type,
                    object_id,
                    object_time,
                    plan.token_hashes,
                )
        except Exception:
            logger.exception("token indexing failed; queued for retry")
            self.enqueue(
                wax_id,
                JOB_INDEX_TOKENS,
                object_type,
                object_id,
            )
        for job in plan.jobs:
            self.enqueue(wax_id, job.kind, object_type, object_id)

    def enqueue(
        self, wax_id: WaxId, kind: str, object_type: ObjectType, object_id: UUID
    ) -> None:
        now = self._clock.now()
        try:
            self._storage.enqueue_job(
                self._new_job(wax_id, kind, object_type, object_id, now)
            )
        except Exception:
            logger.exception("could not queue %s job", kind)

    def backfill_embedding_jobs(
        self,
        wax_id: WaxId,
        objects: tuple[tuple[ObjectType, UUID, datetime], ...],
    ) -> int:
        """Plan and enqueue embedding jobs for objects saved without an embedder.

        Called when an embedding provider is added later. Returns the number
        of jobs enqueued. Objects that already have an embedding row are not
        re-enqueued — callers should filter them before calling.
        """
        if not self._has_embedder:
            return 0
        count = 0
        for object_type, object_id, _object_time in objects:
            self.enqueue(wax_id, JOB_EMBED, object_type, object_id)
            count += 1
        return count

    def _new_job(
        self,
        wax_id: WaxId,
        kind: str,
        object_type: ObjectType,
        object_id: UUID,
        now: datetime,
    ) -> DerivationJob:
        return DerivationJob(
            id=new_id(),
            wax_id=wax_id,
            kind=kind,
            object_type=object_type,
            object_id=object_id,
            status=JobStatus.PENDING,
            attempts=0,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
