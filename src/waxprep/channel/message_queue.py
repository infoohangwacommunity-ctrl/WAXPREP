"""Per-student inbound message queue coordinating rapid message bursts.

Students often send one thought as several rapid messages ("explain
photosynthesis" / "for jss2 please" / "with an example"). The queue groups
whatever arrives inside a short time window into ONE tutor turn, keeps
every message saved verbatim in storage (the handlers save before
submitting), and runs at most one turn per student at a time so
message order is preserved.

Turn assembly is time-based by default: a resettable quiet period plus
a hard collection deadline group bursts into one turn. Operators may
opt in to a small-model semantic grouper that classifies each arrival
(continuation vs new request); it is advisory — every failure falls
back to the time window. There is no keyword matching or phrase routing
here; deciding what the messages MEAN is the model's job, not the
queue's.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from waxprep.channel.contracts import NormalizedInbound
from waxprep.domain.identifiers import WaxId

if TYPE_CHECKING:
    from waxprep.clock import Clock
    from waxprep.model_gateway.gateway import ModelGateway

logger = logging.getLogger(__name__)

# A queued turn runner processes one coalesced batch for one student.
TurnRunner = Callable[[WaxId, list["QueuedMessage"]], Awaitable[None]]


class TurnPhase(StrEnum):
    """Lifecycle of one student's turn inside the queue."""

    COLLECTING = "collecting"
    READY = "ready"
    PROCESSING = "processing"
    COMPLETED = "completed"


class GroupingDecision(StrEnum):
    """What the semantic grouper concluded about a new arrival."""

    JOIN = "join"
    NEW_REQUEST = "new_request"
    UNCERTAIN = "uncertain"


def parse_grouping_decision(text: str | None) -> GroupingDecision:
    """Parse the grouping model's answer leniently.

    The model is asked for one word (``join`` / ``new`` /
    ``uncertain``). Real answers arrive padded, capitalised, quoted, or
    with trailing punctuation; a lenient word match keeps parsing
    robust. Anything unrecognised is UNCERTAIN so the queue's time
    window decides — never a crash, never a stall.
    """
    if not text:
        return GroupingDecision.UNCERTAIN
    words = set(re.findall(r"[a-z]+", text.lower()))
    if "join" in words or "continue" in words or "same" in words:
        return GroupingDecision.JOIN
    if "new" in words:
        return GroupingDecision.NEW_REQUEST
    return GroupingDecision.UNCERTAIN


class SemanticGrouper(Protocol):
    """Advisory classifier: does the arrival continue the pending turn?"""

    async def classify(
        self,
        wax_id: WaxId,
        pending_texts: tuple[str, ...],
        arrival_text: str,
    ) -> GroupingDecision: ...


@dataclass(slots=True)
class QueueConfig:
    """Per-student queue configuration.

    Defaults implement strict per-student ordering: FIFO enforced,
    resettable quiet-period coalescing enabled, exactly one in-flight
    turn per student.

    ``coalesce_window_seconds`` is the resettable quiet period: each
    arrival re-arms it. ``max_collection_seconds`` is the hard deadline
    measured from the FIRST pending message of the batch — the turn
    always starts by then.
    """

    enforce_fifo: bool = True
    coalesce_enabled: bool = True
    coalesce_window_seconds: float = 2.0
    max_batch_messages: int = 5
    max_concurrent_per_student: int = 1
    rate_limit_per_student_per_second: float = 10.0
    worker_idle_timeout_seconds: float = 120.0
    # Hard cap on collection (from the first pending message).
    max_collection_seconds: float = 12.0
    # Opt-in small-model semantic grouping. Time-based assembly remains
    # the default; a grouper must ALSO be wired for this to do anything.
    semantic_grouping_enabled: bool = False
    # Time-box for every grouping call; a slow model must never stall
    # the student's turn. On timeout the time window decides.
    grouping_timeout_seconds: float = 1.5


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


class ModelSemanticGrouper:
    """Small-model semantic grouping for turn assembly.

    A cheap model reads the messages collected so far plus the new
    arrival and classifies the arrival. Any failure — network, gateway,
    malformed answer — returns UNCERTAIN, so the queue's time window
    decides. Grouping is advisory and opt-in; it never gates a student's
    turn.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_name: str = "grouping",
        timeout_seconds: float = 2.0,
        max_output_tokens: int = 24,
    ) -> None:
        self._gateway = gateway
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens

    async def classify(
        self,
        wax_id: WaxId,
        pending_texts: tuple[str, ...],
        arrival_text: str,
    ) -> GroupingDecision:
        # An empty arrival cannot start anything new; treat as JOIN.
        if not arrival_text.strip():
            return GroupingDecision.JOIN
        # Runtime imports keep the gateway contract out of the queue's
        # import graph (and out of every handler's test setup).
        from waxprep.model_gateway.models import (
            ContentItem,
            ContentType,
            ModelRequest,
            ModelRole,
        )

        request = ModelRequest(
            wax_id=wax_id,
            role=ModelRole.GROUPING,
            model=self._model_name,
            instructions=(
                "You classify chat messages for turn assembly. The "
                "COLLECTED block is one thought a student is still "
                "sending. The ARRIVAL is the newest message. Answer "
                "with exactly one word: 'join' if the arrival continues "
                "the collected thought, 'new' if it starts a separate "
                "request, 'uncertain' otherwise."
            ),
            input=(
                ContentItem(
                    type=ContentType.TEXT,
                    text=(
                        "COLLECTED:\n"
                        + "\n".join(f"- {t}" for t in pending_texts)
                        + "\n\nARRIVAL:\n"
                        + arrival_text
                    ),
                ),
            ),
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            response = await self._gateway.complete(request)
        except Exception:
            logger.debug("grouping model call failed; uncertain", exc_info=True)
            return GroupingDecision.UNCERTAIN
        return parse_grouping_decision(response.text)


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
        semantic_grouper: SemanticGrouper | None = None,
    ) -> None:
        self._clock = clock
        self._config = config or QueueConfig()
        self._turn_runner = turn_runner
        self._semantic_grouper = semantic_grouper
        self._queues: dict[WaxId, deque[QueuedMessage]] = {}
        self._workers: dict[WaxId, list[asyncio.Task[None]]] = {}
        self._active: dict[WaxId, int] = {}
        self._recent: dict[WaxId, deque[float]] = {}
        self._phases: dict[WaxId, TurnPhase] = {}
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

    def get_phase(self, wax_id: WaxId) -> TurnPhase | None:
        """The student's current turn phase (observability)."""
        return self._phases.get(wax_id)

    def _set_phase(self, wax_id: WaxId, phase: TurnPhase) -> None:
        self._phases[wax_id] = phase

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
        if not self._config.coalesce_enabled:
            # No collection: each message is its own turn.
            item = queue.popleft()
            item.processing = True
            self._set_phase(wax_id, TurnPhase.READY)
            return [item]
        count = await self._collect_batch(wax_id, queue)
        limit = self._config.max_batch_messages
        batch: list[QueuedMessage] = []
        while queue and len(batch) < count and len(batch) < limit:
            item = queue.popleft()
            item.processing = True
            batch.append(item)
        if len(batch) > 1:
            batch[-1].coalesced_with = [entry.id for entry in batch[:-1]]
        self._set_phase(wax_id, TurnPhase.READY)
        return batch

    async def _collect_batch(self, wax_id: WaxId, queue: deque[QueuedMessage]) -> int:
        """Collect one turn's batch: resettable quiet period + deadline.

        The quiet period re-arms on every arrival; the hard deadline
        (``max_collection_seconds`` from the first pending message)
        always ends collection so the turn starts. When a semantic
        grouper is wired AND enabled, each new arrival is classified:
        a NEW_REQUEST closes the batch without the arrival (it opens
        the next turn); anything else keeps collecting.

        Returns how many of ``queue``'s items may join this batch.
        """
        self._set_phase(wax_id, TurnPhase.COLLECTING)
        window = self._config.coalesce_window_seconds
        hard_cap = self._config.max_collection_seconds
        grouper = (
            self._semantic_grouper if self._config.semantic_grouping_enabled else None
        )
        # The first pending message anchors the batch; classifications
        # start with the second arrival.
        seen = 1
        while queue:
            now = time.monotonic()
            quiet_elapsed = now - queue[-1].received_at >= window
            hard_elapsed = queue[0].received_at + hard_cap <= now
            if quiet_elapsed or hard_elapsed or self._stopped:
                return len(queue)
            if grouper is not None and len(queue) > seen:
                arrival = queue[seen]
                decision = await self._classify_arrival(
                    wax_id, queue, seen, arrival, grouper
                )
                if decision is GroupingDecision.NEW_REQUEST:
                    # Dispatch the collected thought; the new arrival
                    # stays pending and opens the next turn.
                    return seen
                seen = seen + 1
            await asyncio.sleep(0.02)
        return 0

    async def _classify_arrival(
        self,
        wax_id: WaxId,
        queue: deque[QueuedMessage],
        seen: int,
        arrival: QueuedMessage,
        grouper: SemanticGrouper,
    ) -> GroupingDecision:
        pending_texts = tuple(
            (item.message.text or "").strip() for item in list(queue)[:seen]
        )
        arrival_text = (arrival.message.text or "").strip()
        try:
            return await asyncio.wait_for(
                grouper.classify(wax_id, pending_texts, arrival_text),
                timeout=self._config.grouping_timeout_seconds,
            )
        except Exception:
            # The grouper is advisory; the time window decides.
            logger.debug("semantic grouping failed; time window applies", exc_info=True)
            return GroupingDecision.UNCERTAIN

    async def _run_batch(self, wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        self._active[wax_id] = self._active.get(wax_id, 0) + 1
        self._set_phase(wax_id, TurnPhase.PROCESSING)
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
            self._set_phase(wax_id, TurnPhase.COMPLETED)


__all__ = [
    "GroupingDecision",
    "ModelSemanticGrouper",
    "MessageQueue",
    "QueueConfig",
    "QueuedMessage",
    "SemanticGrouper",
    "TurnPhase",
    "TurnRunner",
    "merge_batch_text",
    "parse_grouping_decision",
]
