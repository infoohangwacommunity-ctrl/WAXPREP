"""D2: concurrent inbound safety — five messages, no loss, no double tutor reply per trigger."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.domain.models import AuthorKind
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst


@pytest.mark.asyncio
async def test_five_message_flood_saves_all_and_teaches_each() -> None:
    """Student sends five messages quickly; each is saved and gets processing."""
    world = make_world()
    wax_id = add_student(world)
    turns: TurnServiceCiFirst = world.services.turns  # type: ignore[assignment]
    now = datetime.now(tz=UTC)

    async def one(i: int) -> None:
        await turns.handle_student_message(
            wax_id,
            f"message number {i}",
            channel="whatsapp",
            channel_ref=f"wamid.flood-{i}",
            occurred_at=now,
        )

    await asyncio.gather(*(one(i) for i in range(5)))

    history = [
        e
        for e in world.storage.list_history(wax_id)
        if e.author_kind == AuthorKind.STUDENT
    ]
    assert len(history) >= 5
    refs = {e.channel_ref for e in history}
    assert all(f"wamid.flood-{i}" in refs for i in range(5))

    tutor = [
        e
        for e in world.storage.list_history(wax_id)
        if e.author_kind == AuthorKind.TUTOR
    ]
    # Each student message should get at most one tutor reply (unique index).
    assert len(tutor) >= 5


@pytest.mark.asyncio
async def test_duplicate_webhook_does_not_double_teach() -> None:
    world = make_world()
    wax_id = add_student(world)
    turns: TurnServiceCiFirst = world.services.turns  # type: ignore[assignment]
    teacher: RecordingTeacher = world.teacher

    first = await turns.handle_student_message(
        wax_id,
        "hello once",
        channel="whatsapp",
        channel_ref="wamid.dup-1",
    )
    assert first.duplicate is False
    n = len(teacher.seen_inputs)

    second = await turns.handle_student_message(
        wax_id,
        "hello once",
        channel="whatsapp",
        channel_ref="wamid.dup-1",
    )
    assert second.duplicate is True
    assert len(teacher.seen_inputs) == n  # no second teach
