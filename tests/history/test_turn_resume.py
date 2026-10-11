"""Outbound delivery and crash-recovery tests.

The outbox pattern: the tutor reply is saved first, then an outbound
delivery record is created, then delivery is attempted. A crash between
saving the reply and marking delivered is recoverable: the retry sends
the same stored reply without invoking the Teacher again.

These tests verify the crash-recovery contract end-to-end.
"""

from __future__ import annotations

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.domain.models import AuthorKind
from waxprep.orchestration import TeacherInput, TeacherOutcome


class _CrashingTeacher(RecordingTeacher):
    """Teacher that crashes on the first call, succeeds on retry.

    Simulates a process that stored the student message and then crashed
    before the tutor reply was saved.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        self.calls += 1
        self.seen_inputs.append(inp)
        if self.calls == 1:
            raise RuntimeError("process crashed before saving tutor reply")
        return await super().teach(inp)


@pytest.mark.asyncio
async def test_crash_after_student_message_resumes_and_saves_one_reply() -> None:
    """First delivery saves the student message, then crashes before the
    tutor reply is stored. A retry (duplicate channel delivery) resumes
    and saves exactly one tutor reply.
    """
    world = make_world(teacher=_CrashingTeacher())
    wax = add_student(world)
    channel = "whatsapp"
    channel_ref = "msg-001"
    # First delivery: saves the student message, then the Teacher crashes.
    with pytest.raises(RuntimeError):
        await world.services.turns.handle_student_message(
            wax, "I need help", channel=channel, channel_ref=channel_ref
        )
    # The student message was saved.
    student_history = world.storage.list_history(wax)
    assert len(student_history) == 1
    assert student_history[0].author_kind is AuthorKind.STUDENT
    # Provider retry: the same (channel, channel_ref) is delivered again.
    # This time the Teacher succeeds.
    result = await world.services.turns.handle_student_message(
        wax, "I need help", channel=channel, channel_ref=channel_ref
    )
    assert result.duplicate
    assert result.tutor_entry is not None
    history = world.storage.list_history(wax)
    authors = [e.author_kind for e in history]
    assert authors.count(AuthorKind.STUDENT) == 1
    assert authors.count(AuthorKind.TUTOR) == 1
    assert result.tutor_entry.reply_to_id == result.student_entry.id


class _TeacherCrashesAfterReply(RecordingTeacher):
    """Teacher that returns a reply, then the simulated outbound delivery
    crashes. The reply text IS saved to history; the crash is in the
    post-save outbound path.
    """

    def __init__(self) -> None:
        super().__init__(reply_text="Tutor's first reply")
        self.calls = 0

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        self.calls += 1
        self.seen_inputs.append(inp)
        return await super().teach(inp)


@pytest.mark.asyncio
async def test_crash_after_tutor_reply_saved_resumes_with_same_reply() -> None:
    """A tutor reply is saved to history. The process then crashes before
    the outbound channel delivery. A retry must return the same stored
    reply rather than teaching a second time.
    """
    teacher = _TeacherCrashesAfterReply()
    world = make_world(teacher=teacher)
    wax = add_student(world)
    channel = "whatsapp"
    channel_ref = "msg-002"
    # First delivery: student message + tutor reply both saved.
    first = await world.services.turns.handle_student_message(
        wax, "what is photosynthesis", channel=channel, channel_ref=channel_ref
    )
    assert first.tutor_entry is not None
    assert first.tutor_entry.text == "Tutor's first reply"
    # The provider retries the same delivery because the outbound send
    # never completed (the webhook did not return 200 OK).
    again = await world.services.turns.handle_student_message(
        wax, "what is photosynthesis", channel=channel, channel_ref=channel_ref
    )
    assert again.duplicate
    assert again.tutor_entry is not None
    assert again.tutor_entry.id == first.tutor_entry.id
    assert again.tutor_entry.text == first.tutor_entry.text
    assert teacher.calls == 1, "the Teacher must not be called a second time"
    history = world.storage.list_history(wax)
    authors = [e.author_kind for e in history]
    assert authors.count(AuthorKind.STUDENT) == 1
    assert authors.count(AuthorKind.TUTOR) == 1


@pytest.mark.asyncio
async def test_two_simultaneous_deliveries_one_reply() -> None:
    """Two simultaneous channel deliveries of the same message result in
    exactly one tutor reply.
    """
    teacher = RecordingTeacher()
    world = make_world(teacher=teacher)
    wax = add_student(world)
    channel_ref = "msg-003"
    first = await world.services.turns.handle_student_message(
        wax, "hello", channel="wa", channel_ref=channel_ref
    )
    again = await world.services.turns.handle_student_message(
        wax, "hello", channel="wa", channel_ref=channel_ref
    )
    assert first.tutor_entry is not None
    assert again.tutor_entry is not None
    assert again.tutor_entry.id == first.tutor_entry.id
    assert len(teacher.seen_inputs) == 1
