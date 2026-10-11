"""Job attempt accounting regression tests (spec section 10).

Invariant: one real execution attempt counts exactly once.
- claim/start execution increments attempts exactly once
- success records completion without incrementing attempts again
- failure records the result of that same attempt without incrementing
- lease expiration does not create phantom extra attempts
- retry limits are deterministic
- capability-unavailable jobs do not consume real execution attempts
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    HistoryEntry,
    JobStatus,
    ObjectType,
    Student,
    new_id,
)
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.storage.memory import InMemoryStorage

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _seed_job(storage, wax, *, kind: str = "embed") -> DerivationJob:
    job = DerivationJob(
        id=new_id(),
        wax_id=wax,
        kind=kind,
        object_type=ObjectType.HISTORY,
        object_id=new_id(),
        status=JobStatus.PENDING,
        attempts=0,
        run_after=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    storage.enqueue_job(job)
    return job


def test_claim_increments_attempts_exactly_once() -> None:
    """Claiming a job increments attempts by exactly 1."""
    storage = InMemoryStorage()
    storage.create_student(Student(new_wax_id(), NOW, NOW))
    wax = next(iter(storage._students))  # type: ignore[attr-defined]
    _seed_job(storage, wax)
    claimed = storage.claim_due_jobs(wax, NOW, limit=10)
    assert len(claimed) == 1
    assert claimed[0].attempts == 1, "claim must increment attempts exactly once"


def test_success_does_not_increment_attempts_again() -> None:
    """mark_job_done must not increment attempts (the attempt was counted
    at claim time)."""
    storage = InMemoryStorage()
    storage.create_student(Student(new_wax_id(), NOW, NOW))
    wax = next(iter(storage._students))  # type: ignore[attr-defined]
    _seed_job(storage, wax)
    claimed = storage.claim_due_jobs(wax, NOW, limit=10)
    storage.mark_job_done(wax, claimed[0].id, NOW)
    jobs = storage.list_jobs(wax)
    assert jobs[0].attempts == 1, "success must not increment attempts again"
    assert jobs[0].status is JobStatus.DONE


def test_failure_does_not_increment_attempts_again() -> None:
    """mark_job_failed must not increment attempts (the attempt was counted
    at claim time)."""
    storage = InMemoryStorage()
    storage.create_student(Student(new_wax_id(), NOW, NOW))
    wax = next(iter(storage._students))  # type: ignore[attr-defined]
    _seed_job(storage, wax)
    claimed = storage.claim_due_jobs(wax, NOW, limit=10)
    storage.mark_job_failed(
        wax, claimed[0].id, NOW, "error", NOW + timedelta(seconds=60)
    )
    jobs = storage.list_jobs(wax)
    assert jobs[0].attempts == 1, "failure must not increment attempts again"
    assert jobs[0].status is JobStatus.PENDING


def test_failed_then_retried_counts_two_attempts() -> None:
    """A job that fails and is retried should have attempts=2 after the
    second claim (one for each real execution)."""
    storage = InMemoryStorage()
    storage.create_student(Student(new_wax_id(), NOW, NOW))
    wax = next(iter(storage._students))  # type: ignore[attr-defined]
    _seed_job(storage, wax)
    # First attempt: claim (attempts=1), fail (no increment), re-queue.
    claimed = storage.claim_due_jobs(wax, NOW, limit=10)
    storage.mark_job_failed(
        wax, claimed[0].id, NOW, "error", NOW + timedelta(seconds=60)
    )
    # Advance time past the retry delay.
    later = NOW + timedelta(seconds=120)
    # Second attempt: claim (attempts=2).
    claimed2 = storage.claim_due_jobs(wax, later, limit=10)
    assert len(claimed2) == 1
    assert claimed2[0].attempts == 2, "second claim must increment to 2"


def test_max_attempts_is_deterministic() -> None:
    """With max_attempts=3, the worker gives up after exactly 3 real
    executions (attempts=3)."""
    from waxprep.clock import FakeClock
    from waxprep.derive.worker import DerivationWorker
    from waxprep.index.tokens import TokenIndex

    class AlwaysFails(DeterministicEmbeddingProvider):
        async def embed_many(self, texts: list[str]) -> list[tuple[float, ...]]:
            raise RuntimeError("always fails")

    storage = InMemoryStorage()
    storage.create_student(Student(new_wax_id(), NOW, NOW))
    wax = next(iter(storage._students))  # type: ignore[attr-defined]
    # Create a real history entry so _embed's describe_object finds it.
    entry = storage.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax,
            author_kind=AuthorKind.STUDENT,
            content_kind="text/plain",
            text="embed me",
            occurred_at=NOW,
            recorded_at=NOW,
        )
    )
    job = DerivationJob(
        id=new_id(),
        wax_id=wax,
        kind="embed",
        object_type=ObjectType.HISTORY,
        object_id=entry.id,
        status=JobStatus.PENDING,
        attempts=0,
        run_after=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    storage.enqueue_job(job)
    clock = FakeClock(NOW)
    worker = DerivationWorker(
        storage,
        TokenIndex(TEST_CIPHER),
        clock=clock,
        embedder=AlwaysFails(),
        max_attempts=3,
        retry_base_seconds=0.01,
    )
    import asyncio

    for _ in range(10):
        asyncio.run(worker.run_once())
        clock.advance(100)
    jobs = storage.list_jobs(wax)
    failed = [j for j in jobs if j.status is JobStatus.FAILED]
    assert failed, "job must reach FAILED status after max_attempts"
    assert failed[0].attempts == 3, (
        f"max_attempts=3 must give up after exactly 3 real executions; got {failed[0].attempts}"
    )


def test_capability_skip_does_not_consume_real_attempt() -> None:
    """A capability-unavailable job does not consume a real execution
    attempt. The release_job_capability_skip undoes the claim's increment.
    """
    storage = InMemoryStorage()
    storage.create_student(Student(new_wax_id(), NOW, NOW))
    wax = next(iter(storage._students))  # type: ignore[attr-defined]
    _seed_job(storage, wax, kind="unknown_kind")
    claimed = storage.claim_due_jobs(wax, NOW, limit=10)
    assert claimed[0].attempts == 1
    storage.release_job_capability_skip(
        wax, claimed[0].id, NOW, NOW + timedelta(seconds=600)
    )
    jobs = storage.list_jobs(wax)
    assert jobs[0].attempts == 0, (
        "capability skip must undo the claim's attempt increment"
    )
    assert jobs[0].status is JobStatus.PENDING


def test_expired_lease_can_be_reclaimed() -> None:
    """A job whose lease has expired can be reclaimed by another worker.
    The reclaim increments attempts (counting the new execution).
    """
    storage = InMemoryStorage()
    storage.create_student(Student(new_wax_id(), NOW, NOW))
    wax = next(iter(storage._students))  # type: ignore[attr-defined]
    _seed_job(storage, wax)
    # Worker 1 claims but never completes (lease expires).
    claimed = storage.claim_due_jobs(wax, NOW, limit=10)
    assert claimed[0].attempts == 1
    assert claimed[0].status is JobStatus.RUNNING
    # Time passes; the lease expires.
    later = NOW + timedelta(seconds=600)
    reclaimed = storage.claim_due_jobs(wax, later, limit=10)
    assert len(reclaimed) == 1
    assert reclaimed[0].attempts == 2, (
        "reclaim after lease expiry increments attempts for the new execution"
    )
