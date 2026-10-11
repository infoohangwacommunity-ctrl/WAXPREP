"""Background worker: retries derived work without ever touching history."""

from __future__ import annotations

import logging
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
    JOB_UNDERSTAND_ARTIFACT,
)
from waxprep.index.objects import describe_object
from waxprep.index.tokens import TokenIndex
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


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
        max_attempts: int = 5,
        retry_base_seconds: float = 60.0,
    ) -> None:
        self._storage = storage
        self._token_index = token_index
        self._clock = clock
        self._embedder = embedder
        self._artifacts = artifacts
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds

    def _handlers(self) -> dict[str, Callable[[DerivationJob], Awaitable[None]]]:
        handlers: dict[str, Callable[[DerivationJob], Awaitable[None]]] = {
            JOB_INDEX_TOKENS: self._index_tokens
        }
        if self._embedder is not None:
            handlers[JOB_EMBED] = self._embed
        if self._artifacts is not None:
            handlers[JOB_UNDERSTAND_ARTIFACT] = self._understand
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
                    logger.exception("job %s failed", job.kind)
                    attempts = job.attempts + 1
                    retry_at = (
                        None
                        if attempts >= self._max_attempts
                        else now + timedelta(seconds=self._retry_base * 2**job.attempts)
                    )
                    self._storage.mark_job_failed(
                        wax_id,
                        job.id,
                        now,
                        f"{type(exc).__name__}: {exc}"[:500],
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

    async def _understand(self, job: DerivationJob) -> None:
        assert self._artifacts is not None
        await self._artifacts.understand(job.wax_id, job.object_id)
