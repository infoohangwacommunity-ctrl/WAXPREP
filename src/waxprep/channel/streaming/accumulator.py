"""Authoritative answer accumulator — reconstructs the exact final text
from ordered deltas.

Blueprint §3.2 (Accumulation) and §6.3 (Separate the final answer from
the delivered chunks): the canonical final answer is reconstructed from
the original ordered deltas, NOT by concatenating trimmed outbound
messages. Delivery chunks are derived views of the answer, not the
source of truth.

The previous ``ProgressiveWhatsAppDelivery.final_text`` was:
``"".join(self._emitted) + self._buffer`` — where ``_emitted`` stored
stripped pieces. That loses paragraph boundaries and whitespace. The
``AnswerAccumulator`` fixes this: it appends every delta verbatim and
the final answer is the exact concatenation of all deltas in order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from waxprep.channel.streaming.contract import (
    AnswerStreamEvent,
    AnswerStreamEventType,
    ResponseLifecycle,
)

logger = logging.getLogger(__name__)


@dataclass
class AnswerAccumulator:
    """Reconstructs the exact final answer from ordered deltas.

    The accumulator is the source of truth (blueprint §9.1). Every
    received text delta is appended in order. The final stored answer
    is derived from that complete sequence. Delivery chunks may be
    trimmed, split, reformatted or retried — the accumulator is never
    affected by those operations.

    Invariants:
    - ``final_text`` is always the exact concatenation of all deltas.
    - No delta is ever dropped, trimmed, or reordered.
    - The accumulator does not rewrite educational content.
    - A cancelled response stops accepting deltas.
    """

    response_id: UUID = field(default_factory=uuid4)
    _deltas: list[str] = field(default_factory=list)
    _state: ResponseLifecycle = ResponseLifecycle.PENDING
    _started_at: datetime | None = None
    _completed_at: datetime | None = None
    _sequence: int = 0

    @property
    def state(self) -> ResponseLifecycle:
        return self._state

    @property
    def final_text(self) -> str:
        """The exact concatenation of all deltas, in order. This is the
        authoritative answer — never reconstructed from delivery chunks."""
        return "".join(self._deltas)

    @property
    def delta_count(self) -> int:
        return len(self._deltas)

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def completed_at(self) -> datetime | None:
        return self._completed_at

    def start(self, *, now: datetime | None = None) -> None:
        """Mark the response as ACTIVE. Idempotent — starting an already-
        active response is a no-op."""
        if self._state is ResponseLifecycle.PENDING:
            self._state = ResponseLifecycle.ACTIVE
            self._started_at = now or datetime.now(UTC)

    def append_delta(self, delta: str) -> AnswerStreamEvent:
        """Append one text delta verbatim. Returns the corresponding
        ANSWER_DELTA event for the coordinator.

        Raises if the response is not ACTIVE — a cancelled or completed
        response must not accept new deltas (blueprint §9.4).
        """
        if self._state is not ResponseLifecycle.ACTIVE:
            raise RuntimeError(
                f"cannot append delta to response in state {self._state.value}"
            )
        if not delta:
            return AnswerStreamEvent(
                type=AnswerStreamEventType.ANSWER_DELTA,
                text="",
                response_id=self.response_id,
                sequence=self._sequence,
                timestamp=datetime.now(UTC),
            )
        self._deltas.append(delta)
        self._sequence += 1
        return AnswerStreamEvent(
            type=AnswerStreamEventType.ANSWER_DELTA,
            text=delta,
            response_id=self.response_id,
            sequence=self._sequence,
            timestamp=datetime.now(UTC),
        )

    def complete(self, *, now: datetime | None = None) -> AnswerStreamEvent:
        """Mark the response as COMPLETED. Returns the ANSWER_COMPLETED event."""
        if self._state is ResponseLifecycle.ACTIVE:
            self._state = ResponseLifecycle.COMPLETED
            self._completed_at = now or datetime.now(UTC)
        return AnswerStreamEvent(
            type=AnswerStreamEventType.ANSWER_COMPLETED,
            text=self.final_text,
            response_id=self.response_id,
            sequence=self._sequence,
            timestamp=self._completed_at or datetime.now(UTC),
        )

    def fail(self, *, reason: str, now: datetime | None = None) -> AnswerStreamEvent:
        """Mark the response as FAILED."""
        self._state = ResponseLifecycle.FAILED
        ts = now or datetime.now(UTC)
        return AnswerStreamEvent(
            type=AnswerStreamEventType.RESPONSE_FAILED,
            response_id=self.response_id,
            sequence=self._sequence,
            timestamp=ts,
            metadata={"reason": reason},
        )

    def cancel(self, *, now: datetime | None = None) -> AnswerStreamEvent:
        """Mark the response as CANCELLED. A cancelled response must not
        later publish its stale final message (blueprint §5.6, §9.4)."""
        self._state = ResponseLifecycle.CANCELLED
        ts = now or datetime.now(UTC)
        return AnswerStreamEvent(
            type=AnswerStreamEventType.RESPONSE_CANCELLED,
            response_id=self.response_id,
            sequence=self._sequence,
            timestamp=ts,
        )

    def snapshot(self) -> AnswerStreamEvent:
        """Return the current accumulated text as a snapshot event."""
        return AnswerStreamEvent(
            type=AnswerStreamEventType.ANSWER_SNAPSHOT,
            text=self.final_text,
            response_id=self.response_id,
            sequence=self._sequence,
            timestamp=datetime.now(UTC),
        )


__all__ = ["AnswerAccumulator"]
