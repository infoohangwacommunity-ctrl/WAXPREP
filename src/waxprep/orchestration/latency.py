"""Per-stage turn latency recording — "notes of time" for every turn.

The product owner asked the tutor to take notes of time: how long each
stage of a student's turn takes, durably, without ever blocking or
failing the turn itself.

Design rules:
- Recording is best-effort: a timing failure must never change what the
  student receives or whether the turn succeeds.
- Events are durable and cheap: one storage event per completed turn
  (kind ``turn_latency``), written AFTER the turn's outcome, never on
  the turn's critical path before delivery.
- Stage durations come from the monotonic clock; the event's
  ``created_at`` comes from the injected clock so tests stay
  deterministic.
- No new tables, no migrations: the existing event log
  (``storage.append_event``) carries the payload.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from waxprep.domain.models import Event, new_id

if TYPE_CHECKING:
    from waxprep.clock import Clock
    from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)

EVENT_KIND_TURN_LATENCY = "turn_latency"

# Stage names recorded in the event metadata. Stable strings; downstream
# dashboards rely on them.
STAGE_QUEUE_RECEIVED = "queue_received"
STAGE_BATCH_READY = "batch_ready"
STAGE_TURN_START = "turn_start"
STAGE_CI_START = "ci_start"
STAGE_CI_END = "ci_end"
STAGE_TEACHER_FIRST_DELTA = "teacher_first_delta"
STAGE_FIRST_VISIBLE = "first_visible"
STAGE_REPLY_SAVED = "reply_saved"
STAGE_OUTBOUND_CREATED = "outbound_created"
STAGE_TURN_END = "turn_end"


@dataclass(slots=True)
class TurnLatencyRecord:
    """One turn's timing notes.

    ``marks`` maps a stable stage name to a monotonic timestamp. Marks are
    idempotent (the first wins) so hooks can fire speculatively.
    """

    wax_id: UUID
    turn_id: UUID = field(default_factory=uuid4)
    channel: str | None = None
    trigger_entry_id: UUID | None = None
    marks: dict[str, float] = field(default_factory=dict)
    ci_steps: int = 0
    ci_tool_calls: int = 0

    def mark(self, stage: str, when: float | None = None) -> None:
        """Record a stage timestamp (idempotent: first mark wins)."""
        if stage not in self.marks:
            self.marks[stage] = time.monotonic() if when is None else when

    def seconds_between(self, start: str, end: str) -> float | None:
        try:
            seconds = self.marks[end] - self.marks[start]
        except KeyError:
            return None
        # Notes are forward-looking. A reversed pair means the stages
        # were not marked in order — that is a missing note, not a
        # negative duration.
        return seconds if seconds >= 0 else None


class TurnLatencyRecorder:
    """Collects one turn's timing notes and flushes them best-effort.

    The recorder is per-turn: the turn service holds one record while it
    processes a student message (queue, CI, Teacher, delivery) and
    flushes exactly once, in a ``finally`` block, after the
    student-facing work is done.
    """

    def __init__(self, storage: Storage | None, clock: Clock | None = None) -> None:
        self._storage = storage
        self._clock = clock

    def start(
        self,
        wax_id: UUID,
        *,
        channel: str | None = None,
        trigger_entry_id: UUID | None = None,
    ) -> TurnLatencyRecord:
        record = TurnLatencyRecord(
            wax_id=wax_id, channel=channel, trigger_entry_id=trigger_entry_id
        )
        record.mark(STAGE_TURN_START)
        return record

    def flush(self, record: TurnLatencyRecord) -> None:
        """Persist the record as one event; never raises."""
        record.mark(STAGE_TURN_END)
        try:
            self._emit(record)
        except Exception:
            # Timing must never break a turn; log and move on.
            logger.debug("turn latency flush failed", exc_info=True)

    def _emit(self, record: TurnLatencyRecord) -> None:
        if self._storage is None:
            return
        ci_seconds = record.seconds_between(STAGE_CI_START, STAGE_CI_END)
        first_delta = record.seconds_between(
            STAGE_TURN_START, STAGE_TEACHER_FIRST_DELTA
        )
        visible = record.seconds_between(STAGE_TURN_START, STAGE_FIRST_VISIBLE)
        reply_saved = record.seconds_between(STAGE_TURN_START, STAGE_REPLY_SAVED)
        end_to_end = record.seconds_between(STAGE_TURN_START, STAGE_TURN_END)
        now = self._clock.now() if self._clock is not None else datetime.now(tz=UTC)
        metadata: dict[str, object] = {
            "channel": record.channel,
            "ci_steps": record.ci_steps,
            "ci_tool_calls": record.ci_tool_calls,
            "ci_seconds": ci_seconds,
            "seconds_to_first_delta": first_delta,
            "seconds_to_first_visible": visible,
            "seconds_to_reply_saved": reply_saved,
            "seconds_end_to_end": end_to_end,
            "turn_id": str(record.turn_id),
        }
        self._storage.append_event(
            Event(
                id=new_id(),
                wax_id=record.wax_id,
                kind=EVENT_KIND_TURN_LATENCY,
                object_type="history",
                object_id=record.trigger_entry_id,
                created_at=now,
                metadata=metadata,
            )
        )


__all__ = [
    "EVENT_KIND_TURN_LATENCY",
    "STAGE_BATCH_READY",
    "STAGE_CI_END",
    "STAGE_CI_START",
    "STAGE_FIRST_VISIBLE",
    "STAGE_OUTBOUND_CREATED",
    "STAGE_QUEUE_RECEIVED",
    "STAGE_REPLY_SAVED",
    "STAGE_TEACHER_FIRST_DELTA",
    "STAGE_TURN_END",
    "STAGE_TURN_START",
    "TurnLatencyRecord",
    "TurnLatencyRecorder",
]
