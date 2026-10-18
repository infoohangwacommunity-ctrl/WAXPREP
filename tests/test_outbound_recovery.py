"""Outbound delivery recovery regression tests (spec section 18 / finding 5).

Verifies:
- The tutor reply and its outbound delivery record are persisted
  atomically (in one transaction).
- If a reply exists but its delivery record is missing (simulating a
  crash between reply persistence and outbox creation), a retry
  repairs the missing record and returns the same stored reply without
  invoking the Teacher again.
- The Teacher is never called twice for the same student message.
"""

from __future__ import annotations

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.domain.models import OutboundStatus
from waxprep.orchestration.contracts_ci_first import TeacherInput, TeacherOutcome


class _TrackingTeacher(RecordingTeacher):
    """Teacher that counts how many times teach() is called."""

    def __init__(self, reply: str = "stored reply") -> None:
        super().__init__()
        self.reply_text = reply
        self.teach_calls = 0

    async def teach(self, inp, **kwargs) -> TeacherOutcome:
        self.teach_calls += 1
        self.seen_inputs.append(inp)
        return TeacherOutcome(reply_text=self.reply_text)


@pytest.mark.asyncio
async def test_crash_between_reply_and_outbox_repairs_missing_record() -> None:
    """Simulate a crash after the tutor reply is saved but before the
    outbox record is created. A retry must:
    1. Find the existing reply.
    2. Detect the missing outbox record.
    3. Repair the missing record.
    4. Return the same stored reply.
    5. NOT invoke the Teacher again.
    """
    teacher = _TrackingTeacher()
    world = make_world(teacher=teacher)
    wax = add_student(world)
    channel = "whatsapp"
    channel_ref = "msg-crash-outbox-1"
    # First delivery: save the student message + run the Teacher + save
    # the reply. Then SIMULATE a crash by deleting the outbound
    # delivery record (as if the process crashed after the reply was
    # committed but before the outbox record was committed).
    first = await world.services.turns.handle_student_message(
        wax, "what is photosynthesis", channel=channel, channel_ref=channel_ref
    )
    assert first.tutor_entry is not None
    assert first.outbound_delivery_id is not None
    # Verify the delivery record exists.
    deliveries = world.storage.list_due_outbound_deliveries(
        wax, world.clock.now(), limit=10
    )
    assert any(d.tutor_entry_id == first.tutor_entry.id for d in deliveries)
    # SIMULATE the crash: delete the outbound delivery record.
    world.storage._outbound = {  # type: ignore[attr-defined]
        k: v
        for k, v in world.storage._outbound.items()  # type: ignore[attr-defined]
        if v.tutor_entry_id != first.tutor_entry.id
    }
    # Verify it's gone.
    assert (
        world.storage.get_outbound_delivery_for_tutor(
            wax, first.tutor_entry.id, channel
        )
        is None
    )
    # Retry: the same channel_ref is delivered again.
    again = await world.services.turns.handle_student_message(
        wax, "what is photosynthesis", channel=channel, channel_ref=channel_ref
    )
    assert again.duplicate
    assert again.tutor_entry is not None
    assert again.tutor_entry.id == first.tutor_entry.id, (
        "the same stored reply must be returned"
    )
    # The outbox record must be repaired.
    assert again.outbound_delivery_id is not None, (
        "the missing outbox record must be repaired"
    )
    repaired = world.storage.get_outbound_delivery_for_tutor(
        wax, again.tutor_entry.id, channel
    )
    assert repaired is not None
    assert repaired.status is OutboundStatus.PENDING
    # The Teacher must NOT have been called again.
    assert teacher.teach_calls == 1, (
        f"Teacher must not be invoked again; got {teacher.teach_calls} calls"
    )


@pytest.mark.asyncio
async def test_reply_and_outbox_are_atomic_under_normal_operation() -> None:
    """Under normal operation (no crash), the reply and outbox record are
    both created atomically. The Teacher is called exactly once.
    """
    teacher = _TrackingTeacher()
    world = make_world(teacher=teacher)
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "hello", channel="wa", channel_ref="normal-1"
    )
    assert result.tutor_entry is not None
    assert result.outbound_delivery_id is not None
    assert teacher.teach_calls == 1
    # Verify the delivery record is linked to the tutor entry.
    delivery = world.storage.get_outbound_delivery_for_tutor(
        wax, result.tutor_entry.id, "wa"
    )
    assert delivery is not None
    assert delivery.tutor_entry_id == result.tutor_entry.id
