"""Per-stage latency notes: record, idempotency, durability, resilience.

The turn latency recorder turns "take notes of time" into durable
events: each stage mark is idempotent, flush writes a single storage
event per turn, and a storage failure can never break a student's
turn. Explicit ``when=`` timestamps keep the maths deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from waxprep.orchestration.latency import (
    STAGE_CI_END,
    STAGE_CI_START,
    STAGE_FIRST_VISIBLE,
    STAGE_QUEUE_RECEIVED,
    STAGE_REPLY_SAVED,
    STAGE_TEACHER_FIRST_DELTA,
    STAGE_TURN_END,
    STAGE_TURN_START,
    TurnLatencyRecord,
    TurnLatencyRecorder,
)


class _Storage:
    """Storage double recording appended events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def append_event(self, event: Any) -> Any:
        self.events.append(event)
        return event


class _Clock:
    def __init__(self) -> None:
        self.fixed = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.fixed


def test_mark_is_idempotent_first_write_wins() -> None:
    record = TurnLatencyRecord(wax_id=uuid4(), channel="whatsapp")
    record.mark(STAGE_QUEUE_RECEIVED, when=2.0)
    record.mark(STAGE_QUEUE_RECEIVED, when=5.0)
    assert record.marks[STAGE_QUEUE_RECEIVED] == 2.0
    assert record.seconds_between(STAGE_QUEUE_RECEIVED, STAGE_QUEUE_RECEIVED) == 0.0


def test_seconds_between_computes_deltas_and_missing_is_none() -> None:
    record = TurnLatencyRecord(wax_id=uuid4())
    record.mark(STAGE_QUEUE_RECEIVED, when=1.0)
    record.mark(STAGE_REPLY_SAVED, when=9.5)
    assert record.seconds_between(STAGE_QUEUE_RECEIVED, STAGE_REPLY_SAVED) == 8.5
    assert record.seconds_between(STAGE_REPLY_SAVED, STAGE_QUEUE_RECEIVED) is None
    assert record.seconds_between(STAGE_QUEUE_RECEIVED, "never_marked") is None


def test_flush_appends_one_durable_event() -> None:
    storage = _Storage()
    recorder = TurnLatencyRecorder(storage, _Clock())  # type: ignore[arg-type]
    wax_id = uuid4()
    record = recorder.start(wax_id, channel="telegram")
    record.mark(STAGE_QUEUE_RECEIVED, when=1.0)
    recorder.flush(record)
    assert len(storage.events) == 1
    event = storage.events[0]
    assert event.kind == "turn_latency"
    assert event.wax_id == wax_id
    assert event.object_type == "history"
    assert event.created_at == datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert event.metadata["channel"] == "telegram"
    assert event.metadata["turn_id"] == str(record.turn_id)
    assert event.metadata["ci_seconds"] is None
    assert event.metadata["seconds_to_first_delta"] is None


def test_flush_populates_stage_deltas() -> None:
    storage = _Storage()
    recorder = TurnLatencyRecorder(storage, _Clock())  # type: ignore[arg-type]
    record = recorder.start(uuid4(), channel="whatsapp")
    record.ci_steps = 4
    record.ci_tool_calls = 7
    record.mark(STAGE_CI_START, when=10.0)
    record.mark(STAGE_CI_END, when=13.0)
    record.mark(STAGE_TEACHER_FIRST_DELTA, when=14.0)
    record.mark(STAGE_FIRST_VISIBLE, when=14.5)
    record.mark(STAGE_REPLY_SAVED, when=16.0)
    recorder.flush(record)
    metadata = storage.events[0].metadata
    assert metadata["ci_steps"] == 4
    assert metadata["ci_tool_calls"] == 7
    assert metadata["ci_seconds"] == 3.0
    turn_start = record.marks[STAGE_TURN_START]
    assert metadata["seconds_to_first_delta"] == pytest.approx(14.0 - turn_start)
    assert metadata["seconds_to_first_visible"] == pytest.approx(14.5 - turn_start)
    assert metadata["seconds_to_reply_saved"] == pytest.approx(16.0 - turn_start)
    assert metadata["seconds_end_to_end"] == pytest.approx(
        record.marks[STAGE_TURN_END] - turn_start
    )


def test_flush_never_raises_on_storage_failure() -> None:
    class _Broken:
        def append_event(self, event: Any) -> Any:
            raise RuntimeError("storage down")

    recorder = TurnLatencyRecorder(_Broken(), _Clock())  # type: ignore[arg-type]
    record = recorder.start(uuid4())
    record.mark(STAGE_QUEUE_RECEIVED, when=1.0)
    recorder.flush(record)  # must not raise


def test_flush_without_storage_is_noop() -> None:
    recorder = TurnLatencyRecorder(None, _Clock())
    record = recorder.start(uuid4())
    record.mark(STAGE_QUEUE_RECEIVED, when=1.0)
    recorder.flush(record)  # must not raise
