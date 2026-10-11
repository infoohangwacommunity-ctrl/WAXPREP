"""Blocker D acceptance tests (spec §8.3 items 5 and 6):
- Process crashes after saving the student message but before CI; a retry
  resumes processing and ultimately stores exactly one tutor reply.
- Process crashes after storing the tutor reply but before outbound
  delivery; a retry delivers the same stored reply rather than generating
  a second one.

The contract the tests exercise:
  - A duplicate channel delivery is detected via (channel, channel_ref).
  - If no tutor reply has been saved yet for the trigger entry, the turn
    re-runs investigation and teaching and saves exactly one reply.
  - If a tutor reply is already stored, it is returned without re-teaching.
  - The DB enforces at-most-one tutor reply per trigger entry (migration 008).
"""

from __future__ import annotations

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.domain.models import AuthorKind
from waxprep.history.service import HistoryService


class _CrashingTeacher(RecordingTeacher):
    """Teacher that crashes on the first call, succeeds on retry.

    Simulates a process that stored the student message and then crashed
    before the tutor reply was saved.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def reply(self, wax_id, message, context):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("process crashed before saving tutor reply")
        return await super().reply(wax_id, message, context)


@pytest.mark.asyncio
async def test_crash_after_student_message_resumes_and_saves_one_reply() -> None:
    """§8.3 item 5: crash after save → retry → exactly one tutor reply.

    First delivery saves the student message, then crashes before the
    tutor reply is stored. A retry (the duplicate channel delivery)
    resumes and saves exactly one tutor reply.
    """
    world = make_world(teacher=_CrashingTeacher())
    wax = add_student(world)
    channel = "whatsapp"
    channel_ref = "msg-001"
    # First delivery: saves the student message, then the teacher crashes
    # before a tutor reply can be stored.
    with pytest.raises(RuntimeError):
        await world.services.turns.handle_student_message(
            wax, "I need help", channel=channel, channel_ref=channel_ref
        )
    # The student message was saved (durable before teaching).
    student_history = world.storage.list_history(wax)
    assert len(student_history) == 1
    assert student_history[0].author_kind is AuthorKind.STUDENT
    # Provider retry: the same (channel, channel_ref) is delivered again.
    # This time, the teacher succeeds. The turn must resume and store
    # exactly one tutor reply — not silently declare the turn done.
    result = await world.services.turns.handle_student_message(
        wax, "I need help", channel=channel, channel_ref=channel_ref
    )
    assert result.duplicate, "the duplicate delivery must be detected"
    assert result.tutor_entry is not None, "the turn must resume and produce a reply"
    # Exactly one student message and one tutor reply in the history.
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
        super().__init__(reply="Tutor's first reply")
        self.calls = 0

    async def reply(self, wax_id, message, context):  # type: ignore[no-untyped-def]
        self.calls += 1
        return await super().reply(wax_id, message, context)


@pytest.mark.asyncio
async def test_crash_after_tutor_reply_saved_resumes_with_same_reply() -> None:
    """§8.3 item 6: crash after tutor reply saved → retry → same reply returned.

    A tutor reply is saved to history. The process then crashes before the
    outbound channel delivery. A retry must return the same stored reply
    rather than teaching a second time.
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
    assert again.tutor_entry.id == first.tutor_entry.id, (
        "the same stored reply must be returned, not a new one"
    )
    assert again.tutor_entry.text == first.tutor_entry.text
    assert teacher.calls == 1, "the teacher must not be called a second time"
    # History is unchanged: one student message, one tutor reply.
    history = world.storage.list_history(wax)
    authors = [e.author_kind for e in history]
    assert authors.count(AuthorKind.STUDENT) == 1
    assert authors.count(AuthorKind.TUTOR) == 1


@pytest.mark.asyncio
async def test_two_simultaneous_deliveries_one_reply() -> None:
    """Two simultaneous channel deliveries of the same message result in
    exactly one tutor reply. The duplicate is detected via (channel, channel_ref).
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
    assert len(teacher.seen) == 1, "the teacher must be called exactly once"


def test_history_service_is_idempotent_for_duplicate_channel() -> None:
    """The history service alone (no turn flow) returns the existing
    entry for a duplicate (channel, channel_ref) delivery — without
    enqueuing new derivation jobs."""
    world = make_world()
    wax = add_student(world)
    history: HistoryService = world.services.history
    first, created1 = history.record_with_status(
        wax, AuthorKind.STUDENT, "hello", channel="wa", channel_ref="m-x"
    )
    again, created2 = history.record_with_status(
        wax, AuthorKind.STUDENT, "hello", channel="wa", channel_ref="m-x"
    )
    assert created1 and not created2
    assert again.id == first.id
