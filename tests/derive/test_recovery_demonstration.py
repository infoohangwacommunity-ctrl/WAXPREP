"""Multi-turn recovery demonstration.

Simulates the real failure mode the audit asked for:
1. Student message is saved.
2. Teacher / processing crashes before a tutor reply is persisted.
3. A JOB_PROCESS_TURN recovery job re-runs the full CI-first path.
4. Student still receives a correct tutor reply; process_turn is idempotent
   on a second recovery attempt.

This is the durable guarantee: student text is never lost.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    HistoryEntry,
    JobStatus,
    ObjectType,
    new_id,
)
from waxprep.index.indexer import JOB_PROCESS_TURN
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput as TeacherInputCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherOutcome as TeacherOutcomeCiFirst,
)


class _CrashingTeacher(RecordingTeacher):
    """Teacher that fails on the first call, succeeds on subsequent calls."""

    def __init__(self) -> None:
        super().__init__(reply_text="Recovered tutor reply about fractions.")
        self.fail_next = True

    async def teach(
        self,
        inp: TeacherInputCiFirst,
        *,
        on_text_delta: object | None = None,
    ) -> TeacherOutcomeCiFirst:
        self.seen_inputs.append(inp)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated mid-turn crash before tutor reply")
        if on_text_delta is not None and self.reply_text:
            await on_text_delta(self.reply_text)  # type: ignore[operator]
        return TeacherOutcomeCiFirst(reply_text=self.reply_text)


@pytest.mark.asyncio
async def test_mid_turn_crash_then_process_turn_recovery() -> None:
    """Kill processing after student save → recovery produces the tutor reply."""
    world = make_world()
    crashing = _CrashingTeacher()
    world.services.turns._teacher = crashing  # type: ignore[attr-defined]
    wax_id = add_student(world)
    storage = world.storage
    now = datetime.now(tz=UTC)

    student_entry = storage.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.STUDENT,
            content_kind="text/plain",
            text="Help me with fractions please",
            occurred_at=now,
            recorded_at=now,
            channel="whatsapp",
            channel_ref="wamid.crash-demo-1",
        )
    )

    with pytest.raises(RuntimeError, match="simulated mid-turn crash"):
        await world.services.turns.handle_student_message(
            wax_id,
            student_entry.text or "",
            channel="whatsapp",
            channel_ref=student_entry.channel_ref,
            occurred_at=student_entry.occurred_at,
        )

    assert storage.find_tutor_reply_to(wax_id, student_entry.id) is None

    job = DerivationJob(
        id=new_id(),
        wax_id=wax_id,
        kind=JOB_PROCESS_TURN,
        object_type=ObjectType.HISTORY,
        object_id=student_entry.id,
        status=JobStatus.PENDING,
        attempts=0,
        run_after=now,
        created_at=now,
        updated_at=now,
        last_error="processing_failed: RuntimeError",
    )
    result = await world.services.turns.process_turn_job(job)

    assert result.tutor_entry is not None
    assert result.tutor_entry.text
    assert storage.find_tutor_reply_to(wax_id, student_entry.id) is not None
    assert crashing.seen_inputs
    last = crashing.seen_inputs[-1]
    assert last.original_message.text == "Help me with fractions please"

    calls_before = len(crashing.seen_inputs)
    again = await world.services.turns.process_turn_job(job)
    assert again.duplicate is True
    assert again.tutor_entry is not None
    assert again.tutor_entry.id == result.tutor_entry.id
    assert len(crashing.seen_inputs) == calls_before


@pytest.mark.asyncio
async def test_worker_process_turn_handler_recovers_saved_message() -> None:
    """DerivationWorker._process_turn delegates to TurnServiceCiFirst."""
    world = make_world()
    wax_id = add_student(world)
    storage = world.storage
    now = datetime.now(tz=UTC)
    student_entry = storage.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.STUDENT,
            content_kind="text/plain",
            text="What is a molecule?",
            occurred_at=now,
            recorded_at=now,
            channel="whatsapp",
            channel_ref="wamid.worker-recover",
        )
    )
    job = DerivationJob(
        id=new_id(),
        wax_id=wax_id,
        kind=JOB_PROCESS_TURN,
        object_type=ObjectType.HISTORY,
        object_id=student_entry.id,
        status=JobStatus.PENDING,
        attempts=0,
        run_after=now,
        created_at=now,
        updated_at=now,
    )
    await world.services.worker._process_turn(job)  # type: ignore[attr-defined]
    tutor = storage.find_tutor_reply_to(wax_id, student_entry.id)
    assert tutor is not None
    assert tutor.text
