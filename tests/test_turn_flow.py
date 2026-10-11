"""The whole connected flow: save -> investigate -> teach -> save -> derive."""

from __future__ import annotations

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ContextModelFinal
from waxprep.context.models import ContextPackage
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, HistoryEntry


@pytest.mark.asyncio
async def test_full_turn_saves_both_sides_and_links_the_reply() -> None:
    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "I don't get quadratic equations", channel="whatsapp", channel_ref="w1"
    )
    assert result.tutor_entry is not None
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT, AuthorKind.TUTOR]
    assert result.tutor_entry.reply_to_id == result.student_entry.id
    assert result.context.as_of == world.clock.now()


class _CheckingTeacher(RecordingTeacher):
    def __init__(self, storage: object) -> None:
        super().__init__()
        self.storage = storage
        self.student_was_already_saved = False

    async def reply(
        self, wax_id: WaxId, message: HistoryEntry, context: ContextPackage
    ) -> str:
        found = self.storage.get_history_entry(wax_id, message.id)  # type: ignore[attr-defined]
        self.student_was_already_saved = found is not None
        return "ok"


@pytest.mark.asyncio
async def test_student_message_is_saved_before_the_teacher_runs() -> None:
    world = make_world()
    teacher = _CheckingTeacher(world.storage)
    world = make_world(storage=world.storage, teacher=teacher)
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "hello")
    assert teacher.student_was_already_saved


class _ExplodingTeacher(RecordingTeacher):
    async def reply(
        self, wax_id: WaxId, message: HistoryEntry, context: ContextPackage
    ) -> str:
        raise RuntimeError("teacher provider down")


@pytest.mark.asyncio
async def test_teacher_failure_never_loses_the_students_message() -> None:
    world = make_world(teacher=_ExplodingTeacher())
    wax = add_student(world)
    with pytest.raises(RuntimeError):
        await world.services.turns.handle_student_message(wax, "please help")
    assert [e.text for e in world.storage.list_history(wax)] == ["please help"]


class _ExplodingModel(MockContextModel):
    async def decide(self, request):  # type: ignore[no-untyped-def,override]
        raise RuntimeError("context model down")


@pytest.mark.asyncio
async def test_context_failure_still_gets_the_student_taught() -> None:
    world = make_world(context_model=_ExplodingModel())
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "hello again")
    assert "context_unavailable" in result.context.uncertainty
    assert result.tutor_entry is not None


@pytest.mark.asyncio
async def test_duplicate_channel_delivery_gets_one_reply_only() -> None:
    world = make_world()
    wax = add_student(world)
    first = await world.services.turns.handle_student_message(
        wax, "hi", channel="wa", channel_ref="m1"
    )
    again = await world.services.turns.handle_student_message(
        wax, "hi", channel="wa", channel_ref="m1"
    )
    assert again.duplicate and not first.duplicate
    assert len(world.teacher.seen) == 1
    assert len(world.storage.list_history(wax)) == 2


@pytest.mark.asyncio
async def test_old_exam_remark_is_shown_to_ci_with_its_age() -> None:
    model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "exam"}, "look for exam talk"),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            "old exam remark found", (), ("may_be_out_of_date",), "done"
        ),
    )
    world = make_world(context_model=model)
    wax = add_student(world)
    world.services.history.record(wax, AuthorKind.STUDENT, "my exam is in three months")
    world.clock.advance(150 * 86400)
    await world.services.turns.handle_student_message(wax, "how is my exam prep going?")
    seen = [r for r in model.requests if "my exam is in three months" in r.evidence]
    assert seen, "CI must be shown the old remark"
    assert "months ago" in seen[-1].evidence
    assert "2026-01-01" in seen[-1].evidence
    assert seen[-1].now == world.clock.now()


@pytest.mark.asyncio
async def test_investigation_is_audited_without_conversation_ids() -> None:
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "hello")
    # in-memory storage keeps the audit rows; they point at the triggering entry
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 1 and records[0].trigger_entry_id is not None
