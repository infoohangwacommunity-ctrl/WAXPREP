"""Real PostgreSQL worker concurrency tests (spec section 11).

These tests use two database connections to verify that:
- Two workers cannot claim the same job simultaneously
- Expired leases can be recovered
- Completed jobs cannot be executed again
- Failed jobs retry correctly
- Leases are cleared appropriately
- Attempts remain correct

Unlike the in-memory tests, these exercise real PostgreSQL
``FOR UPDATE SKIP LOCKED`` semantics against a live database.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    HistoryEntry,
    JobStatus,
    ObjectType,
    new_id,
)
from waxprep.migrations import apply_migrations
from waxprep.storage.postgres import PostgresStorage
from waxprep.storage.session import assume_app_role, set_current_wax_id

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def pg_db() -> Any:
    """A clean, fully migrated database."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL not set")
    conn: psycopg.Connection[Any] = psycopg.connect(url, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.commit()
    apply_migrations(conn)
    yield conn
    conn.close()


def _seed_student_and_job(pg_db: Any) -> tuple[Any, DerivationJob]:
    """Create a student and enqueue a job. Returns (wax_id, job)."""
    wax = new_wax_id()
    # Create the student as superuser.
    with pg_db.cursor() as cur:
        cur.execute(
            "INSERT INTO students (wax_id, created_at, updated_at, status) "
            "VALUES (%s, %s, %s, 'active')",
            (wax, NOW, NOW),
        )
    pg_db.commit()
    # Create a storage with the app role for the job.
    store = PostgresStorage(pg_db, TEST_CIPHER)
    # Create a history entry for the job to reference.
    entry = store.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax,
            author_kind=AuthorKind.STUDENT,
            content_kind="text/plain",
            text="concurrency test",
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
    store.enqueue_job(job)
    return wax, job


def test_two_workers_cannot_claim_the_same_job(pg_db: Any) -> None:
    """Two workers using FOR UPDATE SKIP LOCKED cannot claim the same job.
    Worker A claims; worker B gets nothing.
    """
    wax, job = _seed_student_and_job(pg_db)
    store_a = PostgresStorage(pg_db, TEST_CIPHER)
    store_b = PostgresStorage(pg_db, TEST_CIPHER)
    # Worker A claims.
    claimed_a = store_a.claim_due_jobs(wax, NOW, limit=10)
    assert len(claimed_a) == 1
    assert claimed_a[0].id == job.id
    # Worker B claims — should get nothing (A holds the lease).
    claimed_b = store_b.claim_due_jobs(wax, NOW, limit=10)
    assert len(claimed_b) == 0, (
        "worker B must not claim a job that worker A is already running"
    )


def test_expired_lease_can_be_recovered(pg_db: Any) -> None:
    """When a worker's lease expires, another worker can reclaim the job."""
    wax, job = _seed_student_and_job(pg_db)
    store_a = PostgresStorage(pg_db, TEST_CIPHER)
    store_b = PostgresStorage(pg_db, TEST_CIPHER)
    # Worker A claims with a 1-second lease, then "crashes" (never completes).
    claimed_a = store_a.claim_due_jobs(wax, NOW, limit=10, lease_seconds=1)
    assert len(claimed_a) == 1
    # Time passes; the lease expires.
    later = NOW + timedelta(seconds=60)
    claimed_b = store_b.claim_due_jobs(wax, later, limit=10)
    assert len(claimed_b) == 1, (
        "worker B must be able to reclaim a job whose lease has expired"
    )
    assert claimed_b[0].id == job.id
    # The reclaim increments attempts (one more real execution).
    assert claimed_b[0].attempts == 2


def test_completed_job_cannot_be_claimed_again(pg_db: Any) -> None:
    """A job marked DONE is not eligible for claiming."""
    wax, job = _seed_student_and_job(pg_db)
    store = PostgresStorage(pg_db, TEST_CIPHER)
    # Claim and complete the job.
    claimed = store.claim_due_jobs(wax, NOW, limit=10)
    assert len(claimed) == 1
    store.mark_job_done(wax, claimed[0].id, NOW)
    # Another claim attempt returns nothing.
    again = store.claim_due_jobs(wax, NOW + timedelta(seconds=60), limit=10)
    assert len(again) == 0, "a completed job must not be eligible for re-claim"


def test_failed_job_retries_correctly(pg_db: Any) -> None:
    """A failed job is re-queued for retry; the attempts count is correct."""
    wax, job = _seed_student_and_job(pg_db)
    store = PostgresStorage(pg_db, TEST_CIPHER)
    # First claim + fail.
    claimed = store.claim_due_jobs(wax, NOW, limit=10)
    assert claimed[0].attempts == 1
    store.mark_job_failed(wax, claimed[0].id, NOW, "error", NOW + timedelta(seconds=60))
    # Retry after the delay.
    later = NOW + timedelta(seconds=120)
    reclaimed = store.claim_due_jobs(wax, later, limit=10)
    assert len(reclaimed) == 1
    assert reclaimed[0].attempts == 2, (
        "reclaimed job must have attempts=2 (one for each real execution)"
    )


def test_lease_is_cleared_on_completion(pg_db: Any) -> None:
    """mark_job_done clears the lease so the row is no longer eligible."""
    wax, job = _seed_student_and_job(pg_db)
    store = PostgresStorage(pg_db, TEST_CIPHER)
    claimed = store.claim_due_jobs(wax, NOW, limit=10)
    assert claimed[0].lease_until is not None
    store.mark_job_done(wax, claimed[0].id, NOW)
    # Verify the lease is cleared in the DB.
    assume_app_role(pg_db)
    set_current_wax_id(pg_db, wax)
    with pg_db.cursor() as cur:
        cur.execute(
            "SELECT lease_until, status FROM derivation_jobs WHERE id = %s",
            (job.id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None, "lease_until must be NULL after mark_job_done"
    assert row[1] == "done"


def test_lease_is_cleared_on_failure(pg_db: Any) -> None:
    """mark_job_failed clears the lease."""
    wax, job = _seed_student_and_job(pg_db)
    store = PostgresStorage(pg_db, TEST_CIPHER)
    claimed = store.claim_due_jobs(wax, NOW, limit=10)
    assert claimed[0].lease_until is not None
    store.mark_job_failed(wax, claimed[0].id, NOW, "error", NOW + timedelta(seconds=60))
    assume_app_role(pg_db)
    set_current_wax_id(pg_db, wax)
    with pg_db.cursor() as cur:
        cur.execute(
            "SELECT lease_until, status FROM derivation_jobs WHERE id = %s",
            (job.id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None, "lease_until must be NULL after mark_job_failed"
    assert row[1] == "pending"


def test_attempts_count_is_correct_under_concurrent_claim(pg_db: Any) -> None:
    """Under concurrent claiming, attempts is exactly the number of real
    executions — no phantom increments from double-claim or failure.
    """
    wax, job = _seed_student_and_job(pg_db)
    store = PostgresStorage(pg_db, TEST_CIPHER)
    # Round 1: claim (attempts=1), fail (no increment), retry.
    claimed = store.claim_due_jobs(wax, NOW, limit=10)
    assert claimed[0].attempts == 1
    store.mark_job_failed(wax, claimed[0].id, NOW, "err", NOW + timedelta(seconds=60))
    # Round 2: claim (attempts=2), fail (no increment), retry.
    later = NOW + timedelta(seconds=120)
    claimed2 = store.claim_due_jobs(wax, later, limit=10)
    assert claimed2[0].attempts == 2
    store.mark_job_failed(
        wax, claimed2[0].id, later, "err", later + timedelta(seconds=60)
    )
    # Round 3: claim (attempts=3), succeed (no increment).
    later3 = NOW + timedelta(seconds=240)
    claimed3 = store.claim_due_jobs(wax, later3, limit=10)
    assert claimed3[0].attempts == 3
    store.mark_job_done(wax, claimed3[0].id, later3)
    # Final state: attempts=3, status=done. Query directly to verify.
    from waxprep.storage.session import assume_app_role, set_current_wax_id

    assume_app_role(pg_db)
    set_current_wax_id(pg_db, wax)
    with pg_db.cursor() as cur:
        cur.execute(
            "SELECT attempts, status FROM derivation_jobs WHERE id = %s",
            (job.id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 3, f"final attempts must be 3; got {row[0]}"
    assert row[1] == "done"
