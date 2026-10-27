"""Per-student inbound message queue coordinating rapid message bursts.

Students often send one thought as several rapid messages ("explain
photosynthesis" / "for jss2 please" / "with an example"). The queue groups
whatever arrives inside a short time window into ONE tutor turn, keeps
every message saved verbatim in storage (the handlers save before
submitting), and runs at most one turn per student at a time so
conversation order is preserved.

Grouping is purely time-window based (arrival cadence). There is no
keyword matching, phrase routing, or content classification here —
deciding what the messages MEAN is the model's job, not the queue's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from waxprep.channel.contracts import NormalizedInbound
from waxprep.domain.identifiers import WaxId

if TYPE_CHECKING:
    from waxprep.clock import Clock

logger = logging.getLogger(__name__)

# A queued turn runner processes one coalesced batch for one student.
TurnRunner = Callable[[WaxId, list["QueuedMessage"]], Awaitable[None]]


@dataclass(slots=True)
class QueueConfig:
    """Per-student queue configuration.

    Defaults implement strict per-student ordering: FIFO enforced,
    time-window coalescing enabled, exactly one in-flight turn per
    student.
    """

    enforce_fifo: bool = True
    coalesce_enabled: bool = True
    coalesce_window_seconds: float = 2.0
    max_batch_messages: int = 5
    max_concurrent_per_student: int = 1
    rate_limit_per_student_per_second: float = 10.0
    worker_idle_timeout_seconds: float = 120.0


@dataclass(slots=True)
class QueuedMessage:
    """One durably-saved inbound message waiting in the queue."""

    id: UUID
    wax_id: WaxId
    message: NormalizedInbound
    received_at: float
    channel: str | None = None
    entry_id: UUID | None = None
    coalesced_with: list[UUID] = field(default_factory=list)
    processing: bool = False
    processed: bool = False


def merge_batch_text(batch: list[QueuedMessage]) -> str:
    """Merge a coalesced batch's message texts in arrival order."""
    parts: list[str] = []
    for item in batch:
        text = (item.message.text or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


class MessageQueue:
    """Coordinate per-student inbound message processing.

    One worker per student drains that student's deque. With
    `enforce_fifo` (default) the single worker guarantees strict
    message order: a batch fully completes before the next starts.
    The queue only coordinates — durable recovery is the handlers'
    JOB_PROCESS_TURN jobs, because messages are always saved before
    being queued.
    """

    def __init__(
        self,
        clock: Clock,
        config: QueueConfig | None = None,
        *,
        turn_runner: TurnRunner | None = None,
    ) -> None:
        self._clock = clock
        self._config = config or QueueConfig()
        self._turn_runner = turn_runner
        self._queues: dict[WaxId, deque[QueuedMessage]] = {}
        self._workers: dict[WaxId, list[asyncio.Task[None]]] = {}
        self._active: dict[WaxId, int] = {}
        self._recent: dict[WaxId, deque[float]] = {}
        self._stopped = False

    async def submit(
        self,
        msg: NormalizedInbound,
        wax_id: WaxId,
        entry_id: UUID | None = None,
        *,
        channel: str | None = None,
    ) -> UUID | None:
        """Enqueue one saved message; return its queue id.

        Returns `None` when the queue declined the message (rate
        limit or stopped) — the caller should then process it inline.
        """
        if self._stopped:
            return None
        now = time.monotonic()
        if self._rate_limited(wax_id, now):
            logger.info(
                "queue rate limit hit; caller should process inline",
                extra={"wax_id": str(wax_id)},
            )
            return None
        item = QueuedMessage(
            id=uuid4(),
            wax_id=wax_id,
            message=msg,
            received_at=now,
            channel=channel,
            entry_id=entry_id,
        )
        self._queues.setdefault(wax_id, deque()).append(item)
        self._ensure_worker(wax_id)
        return item.id

    async def stop(self) -> None:
        """Stop accepting submissions and cancel all workers."""
        self._stopped = True
        tasks = [t for ws in self._workers.values() for t in ws if not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()

    def get_queue_length(self, wax_id: WaxId | None = None) -> int:
        if wax_id is None:
            return sum(len(q) for q in self._queues.values())
        queue = self._queues.get(wax_id)
        return len(queue) if queue is not None else 0

    def get_active_count(self, wax_id: WaxId | None = None) -> int:
        if wax_id is None:
            return sum(self._active.values())
        return self._active.get(wax_id, 0)

    def _rate_limited(self, wax_id: WaxId, now: float) -> bool:
        limit = int(self._config.rate_limit_per_student_per_second)
        if limit <= 0:
            return False
        recent = self._recent.setdefault(wax_id, deque())
        while recent and now - recent[0] > 1.0:
            recent.popleft()
        if len(recent) >= limit:
            return True
        recent.append(now)
        return False

    def _ensure_worker(self, wax_id: WaxId) -> None:
        if self._stopped:
            return
        workers = self._workers.setdefault(wax_id, [])
        workers[:] = [t for t in workers if not t.done()]
        if self._config.enforce_fifo:
            wanted = 1
        else:
            wanted = max(1, self._config.max_concurrent_per_student)
        while len(workers) < wanted:
            workers.append(asyncio.create_task(self._worker(wax_id)))

    async def _worker(self, wax_id: WaxId) -> None:
        idle_timeout = self._config.worker_idle_timeout_seconds
        last_activity = time.monotonic()
        while not self._stopped:
            batch = await self._next_batch(wax_id)
            if batch:
                last_activity = time.monotonic()
                await self._run_batch(wax_id, batch)
                continue
            if time.monotonic() - last_activity >= idle_timeout:
                break
            await asyncio.sleep(0.02)

    async def _next_batch(self, wax_id: WaxId) -> list[QueuedMessage]:
        queue = self._queues.get(wax_id)
        if not queue:
            return []
        if self._config.coalesce_enabled:
            # Hold the batch open for the coalescing window so rapid
            # follow-ups join the same turn. Purely time-based.
            await asyncio.sleep(self._config.coalesce_window_seconds)
        limit = self._config.max_batch_messages if self._config.coalesce_enabled else 1
        batch: list[QueuedMessage] = []
        while queue and len(batch) < limit:
            item = queue.popleft()
            item.processing = True
            batch.append(item)
        if len(batch) > 1:
            batch[-1].coalesced_with = [entry.id for entry in batch[:-1]]
        return batch

    async def _run_batch(self, wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        self._active[wax_id] = self._active.get(wax_id, 0) + 1
        try:
            if self._turn_runner is not None:
                try:
                    await self._turn_runner(wax_id, batch)
                except Exception:
                    # The turn runner owns durable recovery (the handlers
                    # enqueue JOB_PROCESS_TURN); the queue only
                    # coordinates, never retries.
                    logger.exception(
                        "queued turn failed",
                        extra={"wax_id": str(wax_id), "batch_size": len(batch)},
                    )
        finally:
            self._active[wax_id] = max(0, self._active.get(wax_id, 0) - 1)
            for item in batch:
                item.processing = False
                item.processed = True


__all__ = [
    "MessageQueue",
    "QueueConfig",
    "QueuedMessage",
    "TurnRunner",
    "merge_batch_text",
]
