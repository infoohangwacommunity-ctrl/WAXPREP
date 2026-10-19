"""Recovery: process_turn re-runs full CI-first path for a saved message."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from support import make_world
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    HistoryEntry,
    JobStatus,
    ObjectType,
    new_id,
)
from waxprep.index.indexer import JOB_PROCESS_TURN


@pytest.mark.asyncio
async def test_process_turn_creates_tutor_reply_for_saved_student_message() -> None:
    world = make_world()
    storage = world.storage
    wax_id = __import__("support", fromlist=["add_student"]).add_student(world)
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
            channel_ref="wamid.recovery1",
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

    result = await world.services.turns.process_turn_job(job)

    assert result.student_entry.id == student_entry.id
    assert result.tutor_entry is not None
    assert result.tutor_entry.text
    assert storage.find_tutor_reply_to(wax_id, student_entry.id) is not None


@pytest.mark.asyncio
async def test_process_turn_is_idempotent_when_tutor_reply_exists() -> None:
    world = make_world()
    wax_id = __import__("support", fromlist=["add_student"]).add_student(world)
    first = await world.services.turns.handle_student_message(
        wax_id, "What is photosynthesis?", channel="whatsapp", channel_ref="wamid.a"
    )
    assert first.tutor_entry is not None
    now = datetime.now(tz=UTC)
    job = DerivationJob(
        id=new_id(),
        wax_id=wax_id,
        kind=JOB_PROCESS_TURN,
        object_type=ObjectType.HISTORY,
        object_id=first.student_entry.id,
        status=JobStatus.PENDING,
        attempts=0,
        run_after=now,
        created_at=now,
        updated_at=now,
    )
    second = await world.services.turns.process_turn_job(job)
    assert second.duplicate is True
    assert second.tutor_entry is not None
    assert second.tutor_entry.id == first.tutor_entry.id


@pytest.mark.asyncio
async def test_worker_handles_process_turn_job_kind() -> None:
    world = make_world()
    handlers = world.services.worker._handlers()
    assert JOB_PROCESS_TURN in handlers
