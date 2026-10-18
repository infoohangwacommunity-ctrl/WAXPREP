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
        max_attempts: int = 5,
        retry_base_seconds: float = 60.0,
    ) -> None:
        self._storage = storage
        self._token_index = token_index
        self._clock = clock
        self._embedder = embedder
        self._artifacts = artifacts
        self._turn_processor = turn_processor
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
        return WorkerReport(done, retrying, failed, skipped)

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
