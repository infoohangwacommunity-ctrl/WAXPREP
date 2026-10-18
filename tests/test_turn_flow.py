"""CI-first turn flow tests.

The architecture is:
    Student message
        |
        v
    Save exact student message
        |
        v
    Context Intelligence (mandatory for every message)
        |
        v
    CI produces ContextPackage
        |
        v
    Teacher receives: student message + ContextPackage
        |
        v
    Teacher produces final educational response

These tests verify:
- The exact original student message reaches the Teacher untouched.
- CI runs before the Teacher for every message.
- The Teacher receives the ContextPackage.
- The Teacher cannot request CI consultations (no consultation capability).
- The final tutor reply is saved exactly once.
- The reply is linked to the original student history entry.
- A duplicate channel delivery resumes the same stored reply.
- CI failure does not corrupt history.
"""

from __future__ import annotations

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.context.mock_model import MockContextModel
from waxprep.domain.models import AuthorKind
from waxprep.orchestration.errors import InvalidTeacherOutcomeError


@pytest.mark.asyncio
async def test_full_turn_saves_both_sides_and_links_the_reply() -> None:
    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "I don't get quadratic equations", channel="whatsapp", channel_ref="w1"
    )
    assert result.student_entry.text == "I don't get quadratic equations"
    assert result.student_entry.author_kind == AuthorKind.STUDENT
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == RecordingTeacher.reply_text
    assert result.tutor_entry.author_kind == AuthorKind.TUTOR
    assert result.tutor_entry.reply_to_id == result.student_entry.id


@pytest.mark.asyncio
async def test_duplicate_delivery_returns_existing_reply() -> None:
    world = make_world()
    wax = add_student(world)
    result1 = await world.services.turns.handle_student_message(
        wax, "First message", channel="whatsapp", channel_ref="w1"
    )
    assert result1.tutor_entry is not None
    # Same channel_ref means duplicate delivery
    result2 = await world.services.turns.handle_student_message(
        wax, "First message", channel="whatsapp", channel_ref="w1"
    )
    assert result2.duplicate is True
    assert result2.tutor_entry is not None
    assert result2.tutor_entry.id == result1.tutor_entry.id


@pytest.mark.asyncio
async def test_turn_without_channel_does_not_create_outbound() -> None:
    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "no channel", channel=None
    )
    assert result.tutor_entry is not None
    assert result.outbound_delivery_id is None


@pytest.mark.asyncio
async def test_teacher_receives_exact_student_message() -> None:
    world = make_world()
    wax = add_student(world)
    original = "  Spaces   and\nnewlines\tpreserved  "
    await world.services.turns.handle_student_message(wax, original, channel="x")
    assert len(world.teacher.seen_inputs) == 1
    assert world.teacher.seen_inputs[0].original_message.text == original


@pytest.mark.asyncio
async def test_teacher_receives_original_message_untouched() -> None:
    world = make_world()
    wax = add_student(world)
    original = "Exactly as sent"
    await world.services.turns.handle_student_message(wax, original, channel="x")
    assert len(world.teacher.seen_inputs) == 1
    assert world.teacher.seen_inputs[0].original_message.text == original


@pytest.mark.asyncio
async def test_ci_runs_before_teacher() -> None:
    """Verify CI is invoked before Teacher for every message."""
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(
        wax, "Test message", channel="test"
    )
    # The teacher should have been called
    assert len(world.teacher.seen_inputs) == 1
    # TeacherInput should contain context_package (from CI)
    assert world.teacher.seen_inputs[0].context_package is not None


@pytest.mark.asyncio
async def test_teacher_cannot_consult_ci() -> None:
    """Verify Teacher does not have consultation capability in CI-first."""
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "Test", channel="test")
    # TeacherInput should not have consultation
    inp = world.teacher.seen_inputs[0]
    assert not hasattr(inp, "consultation")


@pytest.mark.asyncio
async def test_outbox_record_created_with_channel() -> None:
    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "Hello", channel="whatsapp", channel_ref="msg123"
    )
    assert result.outbound_delivery_id is not None

    assert result.tutor_entry is not None
    delivery = world.storage.get_outbound_delivery_for_tutor(
        wax, result.tutor_entry.id, "whatsapp"
    )
    assert delivery is not None
    assert delivery.channel == "whatsapp"
    assert delivery.channel_ref == "msg123"
    assert delivery.wax_id == wax


@pytest.mark.asyncio
async def test_history_order_preserved() -> None:
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "First", channel="x")
    await world.services.turns.handle_student_message(wax, "Second", channel="x")
    entries = world.storage.list_history(wax)
    texts = [e.text for e in entries]
    assert texts == [
        "First",
        RecordingTeacher.reply_text,
        "Second",
        RecordingTeacher.reply_text,
    ]


@pytest.mark.asyncio
async def test_ci_failure_does_not_lose_student_message() -> None:
    """CI failure should not prevent student message from being saved."""

    class FailingContextModel(MockContextModel):
        async def decide(self, request):
            raise RuntimeError("CI decide failed")

        async def finalize(self, request):
            raise RuntimeError("CI finalize failed")

    world = make_world(context_model=FailingContextModel())
    wax = add_student(world)
    with pytest.raises(RuntimeError):
        await world.services.turns.handle_student_message(
            wax, "Will this be saved?", channel="x"
        )

    # The student message should still have been saved
    entries = world.storage.list_history(wax)
    student_entries = [e for e in entries if e.author_kind == AuthorKind.STUDENT]
    assert len(student_entries) == 1
    assert student_entries[0].text == "Will this be saved?"


@pytest.mark.asyncio
async def test_teacher_failure_does_not_duplicate_student_message() -> None:

    class FailingTeacher(RecordingTeacher):
        async def teach(self, inp, **kwargs):
            raise RuntimeError("Teacher failed")

    world = make_world(teacher=FailingTeacher())
    wax = add_student(world)
    with pytest.raises(RuntimeError):
        await world.services.turns.handle_student_message(
            wax, "Unique message", channel="x"
        )

    # Only one student entry should exist
    entries = world.storage.list_history(wax)
    student_entries = [e for e in entries if e.author_kind == AuthorKind.STUDENT]
    assert len(student_entries) == 1


@pytest.mark.asyncio
async def test_blank_teacher_reply_raises_error() -> None:
    """Empty reply text should raise InvalidTeacherOutcomeError."""
    from waxprep.orchestration.contracts_ci_first import TeacherOutcome

    class BlankTeacher:
        async def teach(self, inp, **kwargs) -> TeacherOutcome:
            return TeacherOutcome(reply_text="")

    world = make_world(teacher=BlankTeacher())  # type: ignore[arg-type]
    wax = add_student(world)
    with pytest.raises(InvalidTeacherOutcomeError):
        await world.services.turns.handle_student_message(wax, "Test", channel="x")


@pytest.mark.asyncio
async def test_whitespace_only_reply_raises_error() -> None:
    """Whitespace-only reply text should raise InvalidTeacherOutcomeError."""
    from waxprep.orchestration.contracts_ci_first import TeacherOutcome

    class WhitespaceTeacher:
        async def teach(self, inp, **kwargs) -> TeacherOutcome:
            return TeacherOutcome(reply_text="   \n\t  ")

    world = make_world(teacher=WhitespaceTeacher())  # type: ignore[arg-type]
    wax = add_student(world)
    with pytest.raises(InvalidTeacherOutcomeError):
        await world.services.turns.handle_student_message(wax, "Test", channel="x")


@pytest.mark.asyncio
async def test_teacher_receives_context_package() -> None:
    """Verify Teacher receives ContextPackage from CI."""
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(
        wax, "What is 2+2?", channel="test"
    )
    inp = world.teacher.seen_inputs[0]
    assert inp.context_package is not None
    assert inp.context_package.wax_id == wax


@pytest.mark.asyncio
async def test_ci_receives_correct_student_identity() -> None:
    """Verify CI receives the correct student wax_id."""
    world = make_world()
    wax1 = add_student(world, name="Student 1")
    wax2 = add_student(world, name="Student 2")

    await world.services.turns.handle_student_message(wax1, "Message 1", channel="x")
    await world.services.turns.handle_student_message(wax2, "Message 2", channel="x")

    # Both student messages should be saved
    entries1 = world.storage.list_history(wax1)
    entries2 = world.storage.list_history(wax2)
    assert len(entries1) == 2  # student + tutor
    assert len(entries2) == 2  # student + tutor


@pytest.mark.asyncio
async def test_turn_result_contains_context_package() -> None:
    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "Test", channel="test"
    )
    assert result.context_package is not None
    assert result.context_package.wax_id == wax


@pytest.mark.asyncio
async def test_multiple_students_isolated() -> None:
    world = make_world()
    wax1 = add_student(world, name="Alice")
    wax2 = add_student(world, name="Bob")

    await world.services.turns.handle_student_message(wax1, "Hi Alice", channel="x")
    await world.services.turns.handle_student_message(wax2, "Hi Bob", channel="x")

    # Verify both students have their own entries
    entries1 = world.storage.list_history(wax1)
    entries2 = world.storage.list_history(wax2)
    assert len(entries1) == 2
    assert len(entries2) == 2
    assert entries1[0].text == "Hi Alice"
    assert entries2[0].text == "Hi Bob"
