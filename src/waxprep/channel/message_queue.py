"""Per-student message queue with FIFO ordering and coalescing support.

This module provides a configurable message queue that:
1. Enforces per-student FIFO ordering
2. Supports optional time-based message coalescing
3. Handles concurrent message bursts safely
4. Integrates with the existing storage layer

The queue is designed to be backward compatible - when disabled, messages
flow through immediately as before.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import new_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from waxprep.channel.contracts import NormalizedInbound
    from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QueueConfig:
    """Configuration for the message queue."""

    # Ordering
    enforce_fifo: bool = False

    # Coalescing
    coalesce_enabled: bool = False
    coalesce_window_seconds: float = 2.0
    coalesce_max_messages: int = 5

    # Throttling
    max_concurrent_per_student: int = 3
    rate_limit_per_student: float = 10.0  # messages per second

    # Timeouts
    queue_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """A message in the queue."""

    id: UUID
    wax_id: WaxId
    message: NormalizedInbound
    received_at: datetime
    coalesced_with: list[UUID] = field(default_factory=list)
    processed: bool = False
    processing: bool = False


class MessageQueue:
    """Per-student message queue with FIFO ordering and coalescing.

    This queue ensures that messages from the same student are processed
    in the order they were received, with optional coalescing of rapid
    message bursts.

    Thread-safe: uses asyncio locks for concurrent access.
    """

    def __init__(
        self,
        storage: Storage,
        clock: Clock,
        config: QueueConfig | None = None,
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._config = config or QueueConfig()

        # Per-student queues
        self._queues: dict[WaxId, deque[QueuedMessage]] = defaultdict(deque)
        self._queue_locks: dict[WaxId, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

        # Rate limiting
        self._last_send_times: dict[WaxId, deque[datetime]] = defaultdict(deque)
        self._rate_lock = asyncio.Lock()

        # Processing state
        self._active_processing: dict[WaxId, int] = defaultdict(int)
        self._processing_lock = asyncio.Lock()

    async def _get_queue_lock(self, wax_id: WaxId) -> asyncio.Lock:
        """Get or create a lock for a specific student's queue."""
        async with self._global_lock:
            if wax_id not in self._queue_locks:
                self._queue_locks[wax_id] = asyncio.Lock()
            return self._queue_locks[wax_id]

    def _check_rate_limit(self, wax_id: WaxId) -> bool:
        """Check if the student has exceeded their rate limit."""
        if self._config.rate_limit_per_student <= 0:
            return True  # No limit

        now = self._clock.now()
        window = timedelta(seconds=1.0 / self._config.rate_limit_per_student)

        # Clean old timestamps
        while self._last_send_times[wax_id] and (
            now - self._last_send_times[wax_id][0] > timedelta(seconds=1.0)
        ):
            self._last_send_times[wax_id].popleft()

        # Check if within limit
        if len(self._last_send_times[wax_id]) >= self._config.rate_limit_per_student:
            return False

        return True

    def _record_send(self, wax_id: WaxId) -> None:
        """Record that a message was sent for rate limiting."""
        self._last_send_times[wax_id].append(self._clock.now())

    async def enqueue(
        self,
        msg: NormalizedInbound,
        wax_id: WaxId,
    ) -> UUID | None:
        """Enqueue a message for processing.

        Returns the message ID if queued, or None if rejected (rate limited).
        """
        if not self._config.enforce_fifo and not self._config.coalesce_enabled:
            # Queue disabled - process immediately
            return None

        # Check rate limit
        if not self._check_rate_limit(wax_id):
            logger.warning(
                "rate limit exceeded",
                extra={"wax_id": str(wax_id)},
            )
            return None

        queued_msg = QueuedMessage(
            id=new_id(),
            wax_id=wax_id,
            message=msg,
            received_at=self._clock.now(),
        )

        async with await self._get_queue_lock(wax_id):
            # Check if we should coalesce
            if self._config.coalesce_enabled:
                await self._maybe_coalesce(wax_id, queued_msg)

            self._queues[wax_id].append(queued_msg)
            self._record_send(wax_id)

            logger.debug(
                "message queued",
                extra={
                    "wax_id": str(wax_id),
                    "message_id": str(queued_msg.id),
                    "queue_length": len(self._queues[wax_id]),
                },
            )

        return queued_msg.id

    async def _maybe_coalesce(
        self,
        wax_id: WaxId,
        new_msg: QueuedMessage,
    ) -> None:
        """Attempt to coalesce the new message with recent messages in the queue."""
        if not self._config.coalesce_enabled:
            return

        now = self._clock.now()
        window = timedelta(seconds=self._config.coalesce_window_seconds)
        max_messages = self._config.coalesce_max_messages

        queue = self._queues[wax_id]

        # Find messages within the coalesce window
        to_coalesce: list[QueuedMessage] = []
        for msg in queue:
            if not msg.processed and not msg.processing:
                if now - msg.received_at <= window:
                    to_coalesce.append(msg)

        # If we have messages to coalesce and haven't exceeded max
        if to_coalesce and len(to_coalesce) + 1 <= max_messages:
            # Mark these messages as coalesced with the new one
            for msg in to_coalesce:
                msg.coalesced_with.append(new_msg.id)
            logger.debug(
                "messages coalesced",
                extra={
                    "wax_id": str(wax_id),
                    "coalesced_count": len(to_coalesce) + 1,
                },
            )

    async def dequeue(
        self,
        wax_id: WaxId,
    ) -> QueuedMessage | None:
        """Dequeue the next message for a student.

        Returns None if queue is empty or student has too many concurrent messages.
        """
        async with self._processing_lock:
            if (
                self._active_processing[wax_id]
                >= self._config.max_concurrent_per_student
            ):
                return None
            self._active_processing[wax_id] += 1

        try:
            async with await self._get_queue_lock(wax_id):
                queue = self._queues[wax_id]

                if not queue:
                    return None

                # Get the oldest unprocessed message
                for msg in queue:
                    if not msg.processed and not msg.processing:
                        msg.processing = True
                        logger.debug(
                            "message dequeued",
                            extra={
                                "wax_id": str(wax_id),
                                "message_id": str(msg.id),
                            },
                        )
                        return msg

                return None
        except Exception:
            async with self._processing_lock:
                self._active_processing[wax_id] -= 1
            raise

    async def mark_processed(
        self,
        wax_id: WaxId,
        message_id: UUID,
    ) -> None:
        """Mark a message as processed."""
        async with self._processing_lock:
            self._active_processing[wax_id] -= 1

        async with await self._get_queue_lock(wax_id):
            queue = self._queues[wax_id]
            for msg in queue:
                if msg.id == message_id:
                    msg.processed = True
                    msg.processing = False
                    logger.debug(
                        "message marked processed",
                        extra={
                            "wax_id": str(wax_id),
                            "message_id": str(message_id),
                        },
                    )
                    break

    async def mark_failed(
        self,
        wax_id: WaxId,
        message_id: UUID,
    ) -> None:
        """Mark a message as failed (will be retried)."""
        async with self._processing_lock:
            self._active_processing[wax_id] -= 1

        async with await self._get_queue_lock(wax_id):
            queue = self._queues[wax_id]
            for msg in queue:
                if msg.id == message_id:
                    msg.processing = False  # Allow retry
                    logger.debug(
                        "message marked failed",
                        extra={
                            "wax_id": str(wax_id),
                            "message_id": str(message_id),
                        },
                    )
                    break

    async def get_coalesced_messages(
        self,
        wax_id: WaxId,
        message_id: UUID,
    ) -> list[QueuedMessage]:
        """Get all messages that were coalesced with the given message."""
        async with await self._get_queue_lock(wax_id):
            queue = self._queues[wax_id]
            result = []
            for msg in queue:
                if msg.id == message_id or message_id in msg.coalesced_with:
                    result.append(msg)
                elif any(msg.id == id for id in msg.coalesced_with):
                    result.append(msg)
            return result

    async def merge_coalesced_text(
        self,
        wax_id: WaxId,
        message_id: UUID,
    ) -> str:
        """Merge the text of all coalesced messages."""
        messages = await self.get_coalesced_messages(wax_id, message_id)
        texts = []
        for msg in messages:
            if msg.message.text:
                texts.append(msg.message.text)
        return "\n\n".join(texts)

    def get_queue_length(self, wax_id: WaxId) -> int:
        """Get the current queue length for a student."""
        return len(self._queues.get(wax_id, deque()))

    def get_active_count(self, wax_id: WaxId) -> int:
        """Get the number of actively processing messages for a student."""
        return self._active_processing.get(wax_id, 0)


__all__ = ["MessageQueue", "QueueConfig", "QueuedMessage"]
