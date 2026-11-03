"""Founder fix #9: history is preserved — nothing is discarded.

The memory architecture redesign's contract is "save everything that
happens, first and unconditionally" (history/service.py docstring).
A 100-message stress test verifies the contract: every student and
tutor message is durably saved, in order, with no silent drops even
when the exchange is long enough that older systems would have
started trimming.

These tests use ``MockTeacher`` (no real LLM calls) and the
``InMemoryStorage`` so they run deterministically offline.
"""

from __future__ import annotations

import pytest

from support import add_student, make_world
from waxprep.domain.models import AuthorKind


@pytest.mark.asyncio
async def test_one_hundred_messages_all_persisted_in_order() -> None:
    """100 student messages → 100 student history rows + 100 tutor rows.

    The previous defect was that older history was silently dropped
    once the live exchange window was full. The redesign's contract
    is that nothing is dropped — age changes findability, never
    existence. This test proves the contract by counting.
    """
    world = make_world()
    wax = add_student(world)
    for i in range(100):
        await world.services.turns.handle_student_message(
            wax, f"student message number {i:03d}", channel="test"
        )
    entries = world.storage.list_history(wax, limit=10000)
    student = [e for e in entries if e.author_kind is AuthorKind.STUDENT]
    tutor = [e for e in entries if e.author_kind is AuthorKind.TUTOR]
    assert len(student) == 100, (
        f"expected 100 student rows, got {len(student)} — history was silently trimmed"
    )
    assert len(tutor) == 100, (
        f"expected 100 tutor rows, got {len(tutor)} — tutor replies were lost"
    )
    # Order is preserved: students and tutors interleave in arrival order.
    expected_texts = []
    for i in range(100):
        expected_texts.append(f"student message number {i:03d}")
        expected_texts.append(world.teacher.reply_text)
    assert [e.text for e in entries] == expected_texts


@pytest.mark.asyncio
async def test_old_messages_still_retrievable_after_many_new_ones() -> None:
    """The first message must remain in storage after 99 more arrive.

    This is the regression guard against "rolling window" storage:
    a system that drops old messages to make room for new ones would
    fail this test because the first message would be gone by turn 100.
    """
    world = make_world()
    wax = add_student(world)
    first_text = "the very first thing the student said, unique token MARKER_FIRST"
    await world.services.turns.handle_student_message(wax, first_text, channel="test")
    for i in range(99):
        await world.services.turns.handle_student_message(
            wax, f"filler message {i:03d}", channel="test"
        )
    all_entries = world.storage.list_history(wax, limit=10000)
    first = next(
        (
            e
            for e in all_entries
            if e.text == first_text and e.author_kind is AuthorKind.STUDENT
        ),
        None,
    )
    assert first is not None, (
        "first student message was discarded by the time 100 arrived"
    )


@pytest.mark.asyncio
async def test_history_count_matches_message_count_exactly() -> None:
    """No off-by-one: 50 messages → exactly 50 student rows. Not 49, not 51."""
    world = make_world()
    wax = add_student(world)
    for i in range(50):
        await world.services.turns.handle_student_message(
            wax, f"msg {i}", channel="test"
        )
    entries = world.storage.list_history(wax, limit=10000)
    students = [e for e in entries if e.author_kind is AuthorKind.STUDENT]
    assert len(students) == 50


@pytest.mark.asyncio
async def test_no_message_discarded_even_when_teacher_fails() -> None:
    """A Teacher failure does NOT lose the student message — the
    save-exact-message-first contract is upheld even when downstream
    processing fails. The student row is always there.
    """

    class FailingTeacher:
        seen: list = []

        async def teach(self, inp, *, on_text_delta=None):
            self.seen.append(inp)
            raise RuntimeError("teacher exploded")

    world = make_world(teacher=FailingTeacher())  # type: ignore[arg-type]
    wax = add_student(world)
    for i in range(5):
        with pytest.raises(RuntimeError):
            await world.services.turns.handle_student_message(
                wax, f"attempt {i}", channel="test"
            )
    entries = world.storage.list_history(wax, limit=10000)
    students = [e for e in entries if e.author_kind is AuthorKind.STUDENT]
    # Each student message was saved before the Teacher failed.
    assert len(students) == 5
    assert [e.text for e in students] == [f"attempt {i}" for i in range(5)]
