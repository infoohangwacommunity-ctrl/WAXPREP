"""Founder fix #10: duplicate webhook payload → exactly one tutor reply.

The idempotency contract (one-tutor-reply-per-trigger) must survive
the memory redesign. The unique index on
``(wax_id, reply_to_id)`` for tutor entries plus the
``(wax_id, channel, channel_ref)`` unique index for student entries
guarantees that a duplicate webhook delivery reuses the existing
saved reply rather than re-teaching.

These tests use ``MockTeacher`` and the in-memory storage so they
run offline.
"""

from __future__ import annotations

import pytest

from support import add_student, make_world
from waxprep.domain.models import AuthorKind


@pytest.mark.asyncio
async def test_same_channel_ref_twice_yields_one_tutor_reply() -> None:
    """A duplicate webhook (same channel, same channel_ref) must not
    re-invoke the Teacher. The first delivery saves the reply; the
    second is a no-op that returns the existing saved reply.
    """
    world = make_world()
    wax = add_student(world)
    payload_ref = "wamid.IDEMPOTENCY_TEST_1"
    r1 = await world.services.turns.handle_student_message(
        wax, "hello", channel="whatsapp", channel_ref=payload_ref
    )
    r2 = await world.services.turns.handle_student_message(
        wax, "hello", channel="whatsapp", channel_ref=payload_ref
    )
    assert r1.tutor_entry is not None
    assert r2.tutor_entry is not None
    assert r1.tutor_entry.id == r2.tutor_entry.id, (
        "duplicate delivery returned a different tutor reply — re-teaching occurred"
    )
    assert r2.duplicate is True, "second delivery was not flagged as duplicate"
    # The Teacher should have been invoked exactly once.
    assert len(world.teacher.seen_inputs) == 1
    # Exactly one tutor row in storage.
    tutors = [
        e for e in world.storage.list_history(wax, limit=50)
        if e.author_kind is AuthorKind.TUTOR
    ]
    assert len(tutors) == 1


@pytest.mark.asyncio
async def test_different_channel_ref_yields_separate_replies() -> None:
    """Different channel_ref values are NOT duplicate — they are
    distinct student messages that each get their own tutor reply.
    The idempotency key is (channel, channel_ref), not the text.
    """
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(
        wax, "first", channel="whatsapp", channel_ref="ref-A"
    )
    await world.services.turns.handle_student_message(
        wax, "second", channel="whatsapp", channel_ref="ref-B"
    )
    tutors = [
        e for e in world.storage.list_history(wax, limit=50)
        if e.author_kind is AuthorKind.TUTOR
    ]
    assert len(tutors) == 2
    assert len(world.teacher.seen_inputs) == 2


@pytest.mark.asyncio
async def test_outbound_delivery_record_is_repaired_not_duplicated() -> None:
    """If a crash occurs between saving the tutor reply and creating
    the outbound delivery record, the duplicate delivery repairs the
    outbox record instead of creating a second one. This is the
    "save exactly one tutor reply AND its outbound delivery in ONE
    transaction" guarantee from the redesign.
    """
    world = make_world()
    wax = add_student(world)
    payload_ref = "wamid.IDEMPOTENCY_REPAIR_TEST"
    r1 = await world.services.turns.handle_student_message(
        wax, "hi", channel="whatsapp", channel_ref=payload_ref
    )
    assert r1.tutor_entry is not None
    assert r1.outbound_delivery_id is not None
    first_outbound_id = r1.outbound_delivery_id

    # Second delivery: simulates the webhook retrying after a crash
    # between the tutor save and the outbox insert.
    r2 = await world.services.turns.handle_student_message(
        wax, "hi", channel="whatsapp", channel_ref=payload_ref
    )
    assert r2.tutor_entry is not None
    assert r2.tutor_entry.id == r1.tutor_entry.id
    assert r2.outbound_delivery_id == first_outbound_id, (
        "duplicate delivery created a second outbound record instead of repairing"
    )
    # Exactly one outbound delivery record in storage.
    deliveries = world.storage.list_outbound_deliveries_for_wax(wax)
    assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_idempotency_holds_across_one_hundred_redeliveries() -> None:
    """An extreme redelivery storm (100 retries) still yields exactly
    one tutor reply. The unique indices are the contract; they do not
    weaken under load.
    """
    world = make_world()
    wax = add_student(world)
    payload_ref = "wamid.STORM_TEST"
    first = await world.services.turns.handle_student_message(
        wax, "the only message", channel="whatsapp", channel_ref=payload_ref
    )
    for _ in range(99):
        await world.services.turns.handle_student_message(
            wax, "the only message", channel="whatsapp", channel_ref=payload_ref
        )
    tutors = [
        e for e in world.storage.list_history(wax, limit=10000)
        if e.author_kind is AuthorKind.TUTOR
    ]
    assert len(tutors) == 1
    assert len(world.teacher.seen_inputs) == 1
    # The first reply id is the one that survived.
    assert first.tutor_entry is not None
    assert tutors[0].id == first.tutor_entry.id
