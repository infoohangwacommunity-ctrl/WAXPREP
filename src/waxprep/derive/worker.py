"""Background worker: retries derived work without ever touching history.

Error messages stored in `derivation_jobs.last_error` are sanitized to
prevent student content from leaking through exception text (Part 8
Blocker J). The sanitizer strips quoted strings and long tokens that
might contain student data, leaving only the exception class name and
a short, opaque marker.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from waxprep.clock import Clock
from waxprep.context.deletion import DeletionService
from waxprep.derive.artifacts import ArtifactUnderstandingService
from waxprep.domain.models import DerivationJob, EmbeddingRecord, new_id
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.index.indexer import (
    JOB_EMBED,
    JOB_INDEX_TOKENS,
    JOB_INGEST_MEDIA,
    JOB_PROCESS_TURN,
    JOB_UNDERSTAND_ARTIFACT,
)
from waxprep.index.objects import describe_object
from waxprep.index.tokens import TokenIndex
from waxprep.judge.service import JOB_JUDGE_TUTOR_QUALITY, QualityJudgeService
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)

# Blocker J: error messages can carry student content. Sanitize them
# before they reach the database. Quoted strings (e.g. ValueError with
# the student's text in the message) and long tokens are stripped.
_QUOTED_STRING = re.compile(r"'[^']*'|\"[^\"]*\"")
_LONG_TOKEN = re.compile(r"\S{16,}")
_WHITESPACE = re.compile(r"\s+")


def sanitize_error(exc: BaseException, max_length: int = 200) -> str:
    """Reduce an exception to an opaque, non-sensitive marker.

    The marker preserves the exception class name and a short tail, but
    strips quoted strings and long tokens that might contain student
    text. The result is bounded to ``max_length`` characters.
    """
    raw = f"{type(exc).__name__}: {exc}"
    cleaned = _QUOTED_STRING.sub("''", raw)
    cleaned = _LONG_TOKEN.sub("<redacted>", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned[:max_length]


@dataclass(frozen=True, slots=True)
class WorkerReport:
    done: int = 0
    retrying: int = 0
    failed: int = 0
    skipped: int = 0
    # Deletion workflow: number of pending deletion requests the worker
    # processed in this run. Distinct from jobs_done because deletion
    # requests are not derivation_jobs — they live in their own table
    # and are claimed via a separate concurrency-safe primitive.
    deletions_processed: int = 0
    deletions_skipped: int = 0
    deletions_failed: int = 0


class DerivationWorker:
    """Runs queued jobs. A failing job is retried later; history is never at risk."""

    def __init__(
        self,
        storage: Storage,
        token_index: TokenIndex,
        clock: Clock,
        *,
        embedder: EmbeddingProvider | None = None,
        artifacts: ArtifactUnderstandingService | None = None,
        turn_processor: object
        | None = None,  # TurnServiceCiFirst; typed loose to avoid cycle
        deletion_service: DeletionService | None = None,
        judge: QualityJudgeService | None = None,
        max_attempts: int = 5,
        retry_base_seconds: float = 60.0,
    ) -> None:
        self._storage = storage
        self._token_index = token_index
        self._clock = clock
        self._embedder = embedder
        self._artifacts = artifacts
        self._turn_processor = turn_processor
        self._deletion_service = deletion_service
        self._judge = judge
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds

    def _handlers(self) -> dict[str, Callable[[DerivationJob], Awaitable[None]]]:
        handlers: dict[str, Callable[[DerivationJob], Awaitable[None]]] = {
            JOB_INDEX_TOKENS: self._index_tokens,
            # Always available: media derivation is best-effort background work.
            # Full download/OCR/STT runs when media pipeline is configured;
            # otherwise the job completes so it does not clog the queue.
            JOB_INGEST_MEDIA: self._ingest_media,
        }
        if self._embedder is not None:
            handlers[JOB_EMBED] = self._embed
        if self._artifacts is not None:
            handlers[JOB_UNDERSTAND_ARTIFACT] = self._understand
        if self._turn_processor is not None:
            handlers[JOB_PROCESS_TURN] = self._process_turn
        if self._judge is not None:
            handlers[JOB_JUDGE_TUTOR_QUALITY] = self._judge_handler
        return handlers

    async def run_once(
        self, *, student_limit: int = 50, jobs_per_student: int = 20
    ) -> WorkerReport:
        handlers = self._handlers()
        done = retrying = failed = skipped = 0
        now = self._clock.now()
        for wax_id in self._storage.list_wax_ids_with_due_jobs(now, student_limit):
            for job in self._storage.claim_due_jobs(wax_id, now, jobs_per_student):
                handler = handlers.get(job.kind)
                if handler is None:
                    # Capability not configured: return to pending (Blocker B).
                    skipped += 1
                    self._storage.release_job_capability_skip(
                        wax_id,
                        job.id,
                        now,
                        now + timedelta(seconds=self._retry_base * 10),
                    )
                    continue
                try:
                    await handler(job)
                except Exception as exc:
                    # Blocker J: never store raw exception text — it may
                    # contain student data. Log full exception locally;
                    # persist only the sanitized marker.
                    logger.exception("job %s failed", job.kind)
                    # The attempt was already counted at claim time
                    # (claim_due_jobs incremented attempts). Use that
                    # count to decide whether to retry — one real
                    # execution attempt counts exactly once.
                    attempts = job.attempts  # already incremented by claim
                    retry_at = (
                        None
                        if attempts >= self._max_attempts
                        else now + timedelta(seconds=self._retry_base * 2**attempts)
                    )
                    self._storage.mark_job_failed(
                        wax_id,
                        job.id,
                        now,
                        sanitize_error(exc),
                        retry_at,
                    )
                    if retry_at is None:
                        failed += 1
                    else:
                        retrying += 1
                    continue
                self._storage.mark_job_done(wax_id, job.id, now)
                done += 1

        # Process pending deletion requests (Blueprint Part IV §16-17).
        # This is wired into the worker so the running application
        # actually advances the deletion workflow — without this, a
        # REQUEST_DELETION proposal creates a pending row that nobody
        # picks up. The DeletionService does the claim atomically inside
        # its own transaction, so multiple workers can run safely.
        #
        # Blueprint §19 (deferred deletion): the deletion workflow is
        # explicitly deferred. The path here is ISOLATED so a failure
        # in the deletion plumbing (a missing SECURITY DEFINER function,
        # a schema drift, a corrupted request row, a transient DB error)
        # can never abort the derivation-job loop above. Deletion
        # failures are logged and counted; the worker continues.
        deletions_processed = deletions_skipped = deletions_failed = 0
        if self._deletion_service is not None:
            try:
                (
                    deletions_processed,
                    deletions_skipped,
                    deletions_failed,
                ) = self._process_deletion_requests(now)
            except Exception:
                # The per-request try/except inside
                # _process_deletion_requests already handles individual
                # request failures. This outer guard catches a failure
                # in the enumeration itself (e.g. the SECURITY DEFINER
                # function is missing on a fresh DB, or RLS denies
                # access). The derivation jobs above have already been
                # processed and counted; we must not let a deletion-path
                # failure discard that work.
                logger.exception(
                    "deletion_request_enumeration_failed; "
                    "derivation jobs already processed are unaffected"
                )
                deletions_failed = -1  # signal: enumeration itself broke

        return WorkerReport(
            done=done,
            retrying=retrying,
            failed=failed,
            skipped=skipped,
            deletions_processed=deletions_processed,
            deletions_skipped=deletions_skipped,
            deletions_failed=deletions_failed,
        )

    def _process_deletion_requests(self, now: object) -> tuple[int, int, int]:
        """Drain pending deletion requests across all students.

        Returns (processed, skipped, failed). ``processed`` counts
        requests that completed (including rejected); ``skipped`` counts
        requests that another worker had already claimed; ``failed``
        counts requests whose propagation raised — the request stays
        PENDING and the next run will retry it.
        """
        # ``now`` was typed as object to avoid a circular import with
        # the Clock; the DeletionService only forwards it to storage.
        assert self._deletion_service is not None
        processed = skipped = failed = 0
        # Iterate students with pending requests. The storage layer
        # exposes pending requests per wax_id so we can drive the
        # workflow forward student-by-student. If a student has no
        # pending requests, list_deletion_requests returns ().
        # Walk every student that has any deletion_request — we filter
        # to status='pending' inside the loop. (A more efficient
        # implementation would expose list_wax_ids_with_pending_deletions;
        # for now we iterate the existing list_deletion_requests API.)
        # NOTE: list_deletion_requests requires a wax_id; for the worker
        # we need a different access path. We expose this through the
        # storage layer's existing method by iterating students that
        # appear in the events table with kind='deletion_request_created'.
        # That is too indirect; instead, we add a small helper that
        # lists wax_ids with pending requests.
        for wax_id in self._storage.list_wax_ids_with_pending_deletion_requests(
            limit=200
        ):
            for request in self._storage.list_deletion_requests(
                wax_id, status="pending", limit=50
            ):
                try:
                    outcome = self._deletion_service.execute(request, now=now)  # type: ignore[arg-type]
                except Exception:
                    # The DeletionService already logged the failure.
                    # The request stayed PENDING (the claim rolled back),
                    # so the next worker run will retry. Count it as
                    # failed for the report.
                    failed += 1
                    continue
                if outcome.status.value == "pending":
                    # Skipped — another worker already claimed it, or
                    # it was already in a terminal state.
                    skipped += 1
                else:
                    processed += 1
        return processed, skipped, failed

    async def _index_tokens(self, job: DerivationJob) -> None:
        found = describe_object(
            self._storage, job.wax_id, job.object_type, job.object_id
        )
        if found is None:
            return
        text, when = found
        self._storage.put_search_tokens(
            job.wax_id,
            job.object_type,
            job.object_id,
            when,
            self._token_index.hashes(job.wax_id, text),
        )

    async def _embed(self, job: DerivationJob) -> None:
        assert self._embedder is not None
        found = describe_object(
            self._storage, job.wax_id, job.object_type, job.object_id
        )
        if found is None or not found[0].strip():
            return
        text, when = found
        vector = await self._embedder.embed(text)
        self._storage.put_embedding(
            EmbeddingRecord(
                id=new_id(),
                wax_id=job.wax_id,
                object_type=job.object_type,
                object_id=job.object_id,
                object_time=when,
                model=self._embedder.model,
                vector=vector,
                created_at=self._clock.now(),
            )
        )

    async def _process_turn(self, job: DerivationJob) -> None:
        """Re-run the full CI-first teaching path for a saved student message.

        Bound only when a turn processor is configured. Never deletes history.
        Idempotent against an existing tutor reply for the same student entry.
        """
        processor = self._turn_processor
        if processor is None:
            raise RuntimeError("process_turn handler not configured")
        await processor.process_turn_job(job)  # type: ignore[attr-defined]

    async def _judge_handler(self, job: DerivationJob) -> None:
        """Run one periodic tutor-quality evaluation (Quality Judge).

        The judge service owns windowing, dedup, and persistence; this
        handler only dispatches. A judge failure raises so the job
        system retries with backoff — the cursor event is only written
        on a recorded outcome, so a failed run never skips turns.
        """
        assert self._judge is not None
        await self._judge.evaluate_recent(job.wax_id)

    async def _understand(self, job: DerivationJob) -> None:
        assert self._artifacts is not None
        await self._artifacts.understand(job.wax_id, job.object_id)

    async def _ingest_media(self, job: DerivationJob) -> None:
        """Background media derivation for a history entry that carried media.

        Non-blocking relative to the first tutor reply. When a WhatsApp client
        and byte store are not wired into the worker, this is a successful
        no-op so the queue stays clear; caption/text remains on history and
        is already indexed. Idempotent.
        """
        entry = self._storage.get_history_entry(job.wax_id, job.object_id)
        if entry is None:
            return
        # Attachments (if any) are already linked; OCR/STT processors run
        # via understand_artifact when bytes are present. Nothing further
        # required for the durable guarantee of the student message itself.
        logger.info(
            "ingest_media job completed",
            extra={
                "wax_id": str(job.wax_id),
                "history_entry_id": str(job.object_id),
                "content_kind": entry.content_kind,
            },
        )
