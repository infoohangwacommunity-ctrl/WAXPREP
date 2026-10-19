"""Provider-neutral worker/scheduler entry point (spec section 19).

This module provides a CLI entry point that can safely invoke:
- derivation processing (embeddings, token indexing, artifact understanding)
- retries
- consolidation
- embedding backfill
- artifact understanding

The scheduler is not tied to a particular cloud provider. It can be run
as a cron job, a systemd timer, a Kubernetes CronJob, or a long-running
process with APScheduler.

Usage:
    python -m waxprep.scheduler run-once
    python -m waxprep.scheduler run-interval --seconds 60

The worker is safe to run concurrently across multiple processes —
PostgreSQL's FOR UPDATE SKIP LOCKED guarantees no two workers claim
the same job.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from waxprep.clock import SystemClock
from waxprep.context.model import ContextModel
from waxprep.derive.artifacts import ArtifactProcessor
from waxprep.derive.consolidation import ConsolidationModel
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.orchestration.contracts_ci_first import Teacher
from waxprep.security.cipher import FieldCipher, load_cipher_from_env
from waxprep.storage.postgres import PostgresStorage
from waxprep.wiring import WaxPrepServices, build_services

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    """Result of one scheduler run."""

    timestamp: datetime
    students_processed: int
    jobs_done: int
    jobs_retrying: int
    jobs_failed: int
    jobs_skipped: int
    duration_seconds: float
    # Deletion workflow: counts from the worker's deletion-request
    # drain. Distinct from jobs_* because deletion requests live in
    # their own table and are claimed via a separate primitive.
    deletions_processed: int = 0
    deletions_skipped: int = 0
    deletions_failed: int = 0


def build_worker_services(
    *,
    database_url: str | None = None,
    cipher: FieldCipher | None = None,
    embedder: EmbeddingProvider | None = None,
    artifact_processor: ArtifactProcessor | None = None,
    consolidation_model: ConsolidationModel | None = None,
    context_model: ContextModel | None = None,
    teacher: Teacher | None = None,
) -> tuple[PostgresStorage, WaxPrepServices]:
    """Build the service graph for the worker.

    The Teacher and ContextModel are optional because the worker does
    not teach — it only processes background derivation jobs. A
    deterministic MockTeacher and MockContextModel are used as
    placeholders so the service graph is complete.
    """
    url = database_url or os.environ.get("DATABASE_URL", "")
    if not url:
        raise ValueError("DATABASE_URL is required")
    # Apply pending migrations on a short-lived connection first.
    try:
        from waxprep.migrations import apply_migrations

        with psycopg.connect(url) as mig_conn:
            applied = apply_migrations(mig_conn)
            if applied:
                logger.info("worker applied migrations: %s", ", ".join(applied))
    except Exception:
        logger.exception("worker migration apply failed")
    conn: psycopg.Connection[object] = psycopg.connect(url, autocommit=False)

    cipher_obj = cipher or load_cipher_from_env()
    storage = PostgresStorage(conn, cipher_obj)
    clock = SystemClock()
    # The worker doesn't teach, but build_services requires a Teacher
    # and ContextModel. Use mocks.
    from waxprep.context.mock_model import MockContextModel
    from waxprep.orchestration.mock_teacher import MockTeacher

    services = build_services(
        storage=storage,
        cipher=cipher_obj,
        clock=clock,
        context_model=context_model or MockContextModel(),
        teacher=teacher if teacher is not None else MockTeacher(),  # type: ignore[arg-type]
        embedder=embedder,
        consolidation_model=consolidation_model,
        artifact_processor=artifact_processor,
    )
    return storage, services


async def run_once(services: WaxPrepServices) -> SchedulerReport:
    """Run one pass of the derivation worker.

    Safe to run concurrently across multiple processes — PostgreSQL's
    FOR UPDATE SKIP LOCKED guarantees no two workers claim the same job.
    Deletion requests are claimed atomically via a conditional UPDATE
    inside the deletion service's transaction, so two workers cannot
    double-propagate the same request either.
    """
    start = time.time()
    worker = services.worker
    report = await worker.run_once()
    return SchedulerReport(
        timestamp=datetime.now(UTC),
        students_processed=0,  # the worker doesn't expose this directly
        jobs_done=report.done,
        jobs_retrying=report.retrying,
        jobs_failed=report.failed,
        jobs_skipped=report.skipped,
        duration_seconds=time.time() - start,
        deletions_processed=report.deletions_processed,
        deletions_skipped=report.deletions_skipped,
        deletions_failed=report.deletions_failed,
    )


def run_once_sync(services: WaxPrepServices) -> SchedulerReport:
    """Synchronous wrapper for run_once."""
    return asyncio.run(run_once(services))


def run_interval(
    services: WaxPrepServices,
    *,
    seconds: int = 60,
    max_iterations: int | None = None,
) -> None:
    """Run the worker on a fixed interval.

    Args:
        seconds: time between runs (minimum 5 seconds).
        max_iterations: stop after this many runs (None = run forever).
    """
    if seconds < 5:
        raise ValueError("interval must be at least 5 seconds")
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        report = run_once_sync(services)
        logger.info(
            "scheduler run: done=%d retrying=%d failed=%d skipped=%d "
            "deletions_processed=%d deletions_skipped=%d deletions_failed=%d "
            "(%.2fs)",
            report.jobs_done,
            report.jobs_retrying,
            report.jobs_failed,
            report.jobs_skipped,
            report.deletions_processed,
            report.deletions_skipped,
            report.deletions_failed,
            report.duration_seconds,
        )
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        time.sleep(seconds)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="WaxPrep background worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run-once", help="Run one pass of the worker")

    interval_parser = subparsers.add_parser(
        "run-interval", help="Run the worker on a fixed interval"
    )
    interval_parser.add_argument(
        "--seconds", type=int, default=60, help="seconds between runs (min 5)"
    )
    interval_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="stop after this many runs (default: run forever)",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("WAXPREP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    storage, services = build_worker_services()
    try:
        if args.command == "run-once":
            report = run_once_sync(services)
            print(
                f"done={report.jobs_done} retrying={report.jobs_retrying} "
                f"failed={report.jobs_failed} skipped={report.jobs_skipped} "
                f"deletions_processed={report.deletions_processed} "
                f"deletions_skipped={report.deletions_skipped} "
                f"deletions_failed={report.deletions_failed} "
                f"({report.duration_seconds:.2f}s)"
            )
            return 0
        if args.command == "run-interval":
            run_interval(
                services,
                seconds=args.seconds,
                max_iterations=args.max_iterations,
            )
            return 0
    finally:
        storage._conn.close()  # type: ignore[attr-defined]
    return 1


if __name__ == "__main__":
    sys.exit(main())
