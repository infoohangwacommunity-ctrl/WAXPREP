"""Tests for the SIGTERM graceful shutdown drain (Fix 1 + Fix 2).

The Founder's brief: every redeploy can abort a mid-turn transaction
because no ``--timeout-graceful-shutdown`` is set on uvicorn and no
signal handler exists in code. The lifespan now exposes a
``_drain_shutdown`` coroutine that:

1. Flips ``_draining`` so webhook ``accept_inbound`` callbacks reject
   new arrivals (the channel adapter returns 503; the provider
   retries the next replica).
2. Waits for the MessageQueue's active turn count to reach zero
   (bounded by a timeout) — in-flight turns complete, they are not
   cancelled mid-transaction.
3. Cancels + awaits the derivation worker task (Fix 2: previously
   fire-and-forget).
4. Stops the message queue and outboxes.
5. Closes every psycopg connection we opened.

These tests exercise the drain directly (no real SIGTERM, no real
uvicorn) so they run in the standard pytest suite without a database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from waxprep.app import (
    _drain_shutdown,
    _set_draining,
    is_draining,
)
from waxprep.channel.contracts import NormalizedInbound
from waxprep.channel.message_queue import MessageQueue, QueueConfig
from waxprep.clock import SystemClock
from waxprep.domain.identifiers import new_wax_id


def _make_inbound(text: str = "hello") -> NormalizedInbound:
    return NormalizedInbound(
        channel="test",
        provider_sender_id="student-1",
        wax_id=None,
        provider_message_id="msg-1",
        occurred_at=datetime.now(UTC),
        text=text,
    )


@pytest.fixture(autouse=True)
def _reset_draining_flag() -> Any:
    """Ensure each test starts with the drain flag off.

    The drain flag is module-level; without a reset, a test that
    triggers the drain would leak ``_draining = True`` into the next
    test and break ``test_health`` (which would report ``"draining"``).
    """
    _set_draining(False)
    yield
    _set_draining(False)


@pytest.mark.asyncio
async def test_sigterm_drain_sets_draining_flag() -> None:
    """The drain flips ``_draining`` so webhooks reject new arrivals.

    This is the application-level counterpart to uvicorn stopping
    accepting new connections: even a webhook already accepted by the
    ASGI server but not yet queued must not start a new turn the
    process will not finish.
    """
    assert not is_draining()
    await _drain_shutdown()
    assert is_draining(), "drain must flip _draining so webhooks return 503"


@pytest.mark.asyncio
async def test_inflight_turns_complete_before_shutdown() -> None:
    """In-flight turns complete; the drain does not cancel them mid-transaction.

    A turn runner that sleeps 0.3s stands in for a real CI + Teacher
    turn. The drain is started while the turn is in flight; the turn
    must finish (the completion event is set) before the drain returns.
    The drain's 25s default is more than enough headroom.
    """
    completed = asyncio.Event()

    async def slow_turn_runner(wax_id: Any, batch: list[Any]) -> None:
        await asyncio.sleep(0.3)
        completed.set()

    queue = MessageQueue(
        SystemClock(),
        # Disable coalescing so the submitted message starts a turn
        # immediately (no 0.5s quiet-period wait).
        QueueConfig(coalesce_enabled=False, max_collection_seconds=0.0),
        turn_runner=slow_turn_runner,
    )

    wax_id = new_wax_id()
    submitted = await queue.submit(_make_inbound("explain photosynthesis"), wax_id)
    assert submitted is not None, "queue accepted the message"

    # Wait until the queue reports the turn as active (in-flight).
    for _ in range(50):
        if queue.get_active_count() > 0:
            break
        await asyncio.sleep(0.01)
    assert queue.get_active_count() > 0, "turn must be in flight before drain starts"

    # Start the drain while the turn is in flight. The 5s timeout is
    # far above the 0.3s the turn needs; if the drain cancels the
    # turn instead of waiting, completed.is_set() stays False.
    await _drain_shutdown(queue=queue, drain_timeout=5.0)

    assert completed.is_set(), (
        "in-flight turn must complete before the drain finishes — "
        "the drain must wait, not cancel mid-transaction"
    )
    assert queue.get_active_count() == 0


@pytest.mark.asyncio
async def test_drain_cancels_and_awaits_worker_task() -> None:
    """Fix 2: the derivation worker task is cancelled + awaited on shutdown.

    The previous finding: ``asyncio.create_task(_worker_loop())`` was
    fire-and-forget — no reference held, no cancellation in the
    lifespan teardown. The drain now holds the task and cancels it
    inside the graceful-shutdown window so a mid-model-call kill is at
    least a clean ``CancelledError``, not an unawaited-task warning.
    """
    worker_cancelled = asyncio.Event()

    async def long_running_worker() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise

    task = asyncio.create_task(long_running_worker())
    # Let the task actually start (otherwise cancel() lands before the
    # sleep is registered and the test is trivial).
    await asyncio.sleep(0.05)
    assert not task.done()

    await _drain_shutdown(worker_task=task, worker_cancel_timeout=2.0)

    assert task.done(), "drain must cancel + await the worker task"
    assert worker_cancelled.is_set(), (
        "worker task must observe CancelledError, not be abandoned"
    )


@pytest.mark.asyncio
async def test_drain_closes_db_connections() -> None:
    """Every psycopg connection we opened is closed on shutdown.

    Without this the process exit leaks connections and the next cold
    start races the old connection's COMMIT/ROLLBACK state. The drain
    reads ``_conn`` off each storage handle and calls ``close()``.
    """
    closed: list[str] = []

    class _FakeConn:
        def close(self) -> None:
            closed.append("conn")

    class _FakeStorage:
        _conn = _FakeConn()

    handles = [_FakeStorage(), _FakeStorage()]
    await _drain_shutdown(storage_handles=handles)

    assert closed == ["conn", "conn"], (
        "drain must close every tracked storage connection"
    )


@pytest.mark.asyncio
async def test_drain_stops_outboxes() -> None:
    """Each outbox's ``stop()`` is awaited (flips ``_running = False``)."""
    stopped: list[str] = []

    class _FakeOutbox:
        async def stop(self) -> None:
            stopped.append("outbox")

    outboxes = [_FakeOutbox(), _FakeOutbox()]
    await _drain_shutdown(outboxes=outboxes)

    assert stopped == ["outbox", "outbox"], "drain must stop every outbox"


@pytest.mark.asyncio
async def test_drain_is_best_effort_on_failures() -> None:
    """A drain failure must never mask the original shutdown signal.

    If one outbox's ``stop()`` raises, the drain must still close DB
    connections and the worker task. The lifespan teardown depends on
    this — a partial drain is better than a hung one.
    """
    db_closed: list[str] = []

    class _BoomOutbox:
        async def stop(self) -> None:
            raise RuntimeError("boom")

    class _FakeConn:
        def close(self) -> None:
            db_closed.append("conn")

    class _FakeStorage:
        _conn = _FakeConn()

    # Should NOT raise despite the outbox failure.
    await _drain_shutdown(
        outboxes=[_BoomOutbox()],
        storage_handles=[_FakeStorage()],
    )

    assert db_closed == ["conn"], (
        "drain must continue past an outbox failure and still close DBs"
    )
