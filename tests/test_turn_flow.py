"""Teacher-first turn flow tests.

The architecture is:

    Student message
        |
        v
    Teacher (drives the turn)
        |   may consult Context Intelligence (bounded)
        v
    Teacher eventually produces the response
        |
        v
    Tutor response is durably saved + outbound delivery recorded

These tests verify:
- The exact original student message reaches the Teacher untouched.
- The Teacher can respond without consulting CI.
- The Teacher can consult CI (bounded).
- The Teacher can request another CI investigation (bounded).
- The consultation limit is enforced.
- CI failure does not corrupt history.
- The final tutor reply is saved exactly once.
- The reply is linked to the original student history entry.
- A duplicate channel delivery resumes the same stored reply.
"""

from __future__ import annotations

import pytest

from support import RecordingTeacher, add_student, make_world
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ContextModelFinal
from waxprep.domain.models import AuthorKind
from waxprep.orchestration import (
    ConsultationRequest,
    InvalidTeacherOutcomeError,
    TeacherInput,
    TeacherOutcome,
)


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
    assert result.outbound_delivery_id is not None


@pytest.mark.asyncio
async def test_exact_student_message_reaches_teacher_untouched() -> None:
    """The exact, original student text must reach the Teacher unchanged.

    No rewriting, summarization, or CI interpretation replaces it.
    """
    world = make_world()
    wax = add_student(world)
    original = "I am hungry after school every day at 4pm"
    await world.services.turns.handle_student_message(wax, original)
    assert world.teacher.seen_inputs, "Teacher must receive at least one input"
    received = world.teacher.seen_inputs[0].original_message
    assert received.text == original, (
        f"Teacher received mutated text: {received.text!r} != {original!r}"
    )
    # The persisted history entry must also match exactly.
    persisted = world.storage.list_history(wax)[0]
    assert persisted.text == original


class _CheckingTeacher(RecordingTeacher):
    """Teacher that checks whether the student message was already saved
    to storage before the Teacher was called.
    """

    def __init__(self, storage: object) -> None:
        super().__init__()
        self.storage = storage
        self.student_was_already_saved = False

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        found = self.storage.get_history_entry(  # type: ignore[attr-defined]
            inp.wax_id, inp.original_message.id
        )
        self.student_was_already_saved = found is not None
        return await super().teach(inp)


@pytest.mark.asyncio
async def test_student_message_is_saved_before_the_teacher_runs() -> None:
    world = make_world()
    teacher = _CheckingTeacher(world.storage)
    world = make_world(storage=world.storage, teacher=teacher)
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "hello")
    assert teacher.student_was_already_saved


class _ExplodingTeacher(RecordingTeacher):
    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        # Record the input first so tests can verify the message was saved.
        self.seen_inputs.append(inp)
        raise RuntimeError("teacher provider down")


@pytest.mark.asyncio
async def test_teacher_failure_never_loses_the_students_message() -> None:
    world = make_world(teacher=_ExplodingTeacher())
    wax = add_student(world)
    with pytest.raises(RuntimeError):
        await world.services.turns.handle_student_message(wax, "please help")
    # The student message was saved before the Teacher ran.
    assert [e.text for e in world.storage.list_history(wax)] == ["please help"]


@pytest.mark.asyncio
async def test_teacher_can_respond_without_ci() -> None:
    """The Teacher can respond directly without consulting CI.

    CI is a capability, not a gatekeeper.
    """
    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "hello")
    assert result.tutor_entry is not None
    assert result.consultations_made == 0
    # No investigation audit recorded.
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 0


@pytest.mark.asyncio
async def test_teacher_can_consult_ci_once() -> None:
    """The Teacher can ask CI one question, then respond."""
    world = make_world(teacher=RecordingTeacher(consult_question="find prior context"))
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "help me")
    assert result.tutor_entry is not None
    assert result.consultations_made == 1
    # The investigation audit was recorded.
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 1
    assert records[0].trigger_entry_id is not None


@pytest.mark.asyncio
async def test_consultation_limit_is_enforced() -> None:
    """The hard upper bound on consultations stops an infinite loop: at
    most max_consultations CI calls happen, and the forced final round
    ends in a controlled failure instead of a persisted empty reply."""
    # A Teacher that always asks another question.
    teacher = RecordingTeacher(reply_text="ok", consult_question="more?")
    # Override teach to never finish on its own — only the bound stops it.

    async def always_consult(inp: TeacherInput) -> TeacherOutcome:
        teacher.seen_inputs.append(inp)

        result = await inp.consultation.consult(
            ConsultationRequest(
                wax_id=inp.wax_id,
                original_message=inp.original_message,
                teacher_question="more?",
                now=inp.now,
                prior_results=inp.prior_results,
            )
        )
        return TeacherOutcome(
            reply_text=None,
            next_question="more?",
            context_used=(*inp.prior_results, result),
            finished=False,
        )

    teacher.teach = always_consult  # type: ignore[assignment]
    world = make_world(teacher=teacher, max_consultations=3)
    wax = add_student(world)
    with pytest.raises(InvalidTeacherOutcomeError):
        await world.services.turns.handle_student_message(wax, "loop me")
    # The bound was hit: at most 3 rounds + 1 forced final round ran.
    assert len(teacher.seen_inputs) <= 4
    # Actual CI calls never exceeded the bound.
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) <= 3
    # No empty tutor entry was persisted.
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT]


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
    assert len(world.teacher.seen_inputs) == 1, "Teacher must be called exactly once"
    assert len(world.storage.list_history(wax)) == 2


@pytest.mark.asyncio
async def test_old_exam_remark_is_shown_to_ci_with_its_age() -> None:
    """The Teacher can ask CI to find old remarks; CI returns them with
    their original date and relative age so the Teacher can tell whether
    they are still current.
    """
    model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "exam"}, "look for exam talk"),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            "old exam remark found", (), ("may_be_out_of_date",), "done"
        ),
    )
    # Teacher asks CI to investigate.
    teacher = RecordingTeacher(consult_question="find prior exam remarks")
    world = make_world(context_model=model, teacher=teacher)
    wax = add_student(world)
    world.services.history.record(wax, AuthorKind.STUDENT, "my exam is in three months")
    world.clock.advance(150 * 86400)
    await world.services.turns.handle_student_message(wax, "how is my exam prep going?")
    # CI must have been shown the old remark with its date and age.
    seen = [r for r in model.requests if "my exam is in three months" in r.evidence]
    assert seen, "CI must be shown the old remark"
    assert "months ago" in seen[-1].evidence
    assert "2026-01-01" in seen[-1].evidence
    assert seen[-1].now == world.clock.now()


@pytest.mark.asyncio
async def test_investigation_is_audited_without_conversation_ids() -> None:
    """When the Teacher asks CI, the investigation is recorded as an audit
    row pointing at the triggering history entry — no conversation ID.
    """
    world = make_world(teacher=RecordingTeacher(consult_question="look up stuff"))
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "hello")
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 1
    assert records[0].trigger_entry_id is not None


@pytest.mark.asyncio
async def test_outbound_delivery_created_after_tutor_reply() -> None:
    """When a tutor reply is saved with a channel, an outbound delivery
    record is created so a crash between save and delivery can be
    recovered without re-invoking the Teacher.
    """
    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "hi", channel="whatsapp", channel_ref="in-1"
    )
    assert result.tutor_entry is not None
    assert result.outbound_delivery_id is not None
    # The delivery record points at the tutor entry.
    deliveries = world.storage.list_due_outbound_deliveries(
        wax, world.clock.now(), limit=10
    )
    assert any(d.tutor_entry_id == result.tutor_entry.id for d in deliveries)


@pytest.mark.asyncio
async def test_crash_after_tutor_reply_saved_resumes_with_same_reply() -> None:
    """If the process crashes after the tutor reply is saved but before
    outbound delivery, the retry must return the same stored reply
    without invoking the Teacher again.
    """
    teacher = RecordingTeacher(reply_text="Tutor's first reply")
    world = make_world(teacher=teacher)
    wax = add_student(world)
    channel_ref = "msg-crash-1"
    first = await world.services.turns.handle_student_message(
        wax, "what is photosynthesis", channel="wa", channel_ref=channel_ref
    )
    assert first.tutor_entry is not None
    # Simulate the outbound delivery never completing (crash). The
    # provider retries the same student delivery.
    again = await world.services.turns.handle_student_message(
        wax, "what is photosynthesis", channel="wa", channel_ref=channel_ref
    )
    assert again.duplicate
    assert again.tutor_entry is not None
    assert again.tutor_entry.id == first.tutor_entry.id, (
        "the same stored reply must be returned, not a new one"
    )
    assert len(teacher.seen_inputs) == 1, "Teacher must not be called again"


# --- response validation regressions -------------------------------------------
#
# These tests guard the forced-final-round path and the final-reply
# validation. Under the previous behaviour
# (``reply_text = outcome.reply_text or ""``) a missing or blank final
# response was coerced into an empty string and persisted as a successful
# tutor reply; these tests fail under that behaviour.


class _OutcomeTeacher(RecordingTeacher):
    """Teacher double that returns a canned outcome on a chosen round."""

    def __init__(
        self,
        outcome: TeacherOutcome,
        *,
        outcome_round: int | None = None,
        always: bool = False,
    ) -> None:
        super().__init__()
        self.outcome = outcome
        self.outcome_round = outcome_round
        self.always = always

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        self.seen_inputs.append(inp)
        if self.always or inp.round_number == self.outcome_round:
            return self.outcome
        return await super().teach(inp)


async def _run_turn(world: object, wax: object, text: str) -> object:
    return await world.services.turns.handle_student_message(wax, text)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_blank_final_reply_is_never_persisted() -> None:
    """A whitespace-only final reply is a controlled failure, not a reply."""
    world = make_world(
        teacher=_OutcomeTeacher(
            TeacherOutcome(
                reply_text="   ", next_question=None, context_used=(), finished=True
            ),
            outcome_round=0,
        )
    )
    wax = add_student(world)
    with pytest.raises(InvalidTeacherOutcomeError):
        await _run_turn(world, wax, "help me")
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT]
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert records == []


@pytest.mark.asyncio
async def test_empty_string_reply_is_never_persisted() -> None:
    world = make_world(
        teacher=_OutcomeTeacher(
            TeacherOutcome(
                reply_text="", next_question=None, context_used=(), finished=True
            ),
            outcome_round=0,
        )
    )
    wax = add_student(world)
    with pytest.raises(InvalidTeacherOutcomeError):
        await _run_turn(world, wax, "help me")
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT]


@pytest.mark.asyncio
async def test_forced_final_round_missing_reply_is_never_persisted() -> None:
    """Regression: in the forced final round, a missing reply must not be
    coerced through ``reply_text or ""`` into an empty persisted entry."""
    teacher = RecordingTeacher(consult_question="look for context")
    world = make_world(teacher=teacher, max_consultations=1)
    wax = add_student(world)
    # Round 0 consults; the forced final round returns finished=True with
    # no reply text.
    teacher_teach = teacher.teach

    async def consult_then_decline(inp: TeacherInput) -> TeacherOutcome:
        if inp.round_number == 0:
            return await teacher_teach(inp)
        return TeacherOutcome(
            reply_text=None,
            next_question=None,
            context_used=inp.prior_results,
            finished=True,
        )

    teacher.teach = consult_then_decline  # type: ignore[assignment]
    with pytest.raises(InvalidTeacherOutcomeError):
        await world.services.turns.handle_student_message(wax, "hello")
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT]
    # No tutor entry with empty text may exist.
    assert all((e.text or "").strip() for e in saved)


@pytest.mark.asyncio
async def test_forced_final_round_unfinished_outcome_is_never_persisted() -> None:
    """Regression: the forced final round must validate the whole outcome —
    an unfinished outcome is a controlled failure, not a success."""
    teacher = RecordingTeacher(consult_question="look for context")
    world = make_world(teacher=teacher, max_consultations=1)
    wax = add_student(world)
    teacher_teach = teacher.teach

    async def consult_then_unfinished(inp: TeacherInput) -> TeacherOutcome:
        if inp.round_number == 0:
            return await teacher_teach(inp)
        return TeacherOutcome(
            reply_text=None,
            next_question="one more?",
            context_used=inp.prior_results,
            finished=False,
        )

    teacher.teach = consult_then_unfinished  # type: ignore[assignment]
    with pytest.raises(InvalidTeacherOutcomeError):
        await world.services.turns.handle_student_message(wax, "hello")
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT]


@pytest.mark.asyncio
async def test_valid_final_reply_after_consultation_still_persists() -> None:
    """The validation must not break the normal path: a real reply after a
    consultation round is still persisted and linked."""
    teacher = RecordingTeacher(consult_question="look for context")
    world = make_world(teacher=teacher, max_consultations=2)
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "hello")
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == teacher.reply_text
    assert result.consultations_made == 1
