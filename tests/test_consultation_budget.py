"""Bounded consultation budget regression tests (spec section 4 / finding 4).

Verifies that the consultation budget is enforced at the
consultation-capability boundary, counting every CI call across the
entire turn — not merely Teacher rounds. A Teacher that attempts
multiple consultations within one ``teach()`` invocation is counted
correctly, and the budget is not exceeded.
"""

from __future__ import annotations

import pytest

from support import add_student, make_world
from waxprep.orchestration import (
    ConsultationRequest,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.storage.memory import InMemoryStorage


class _MultiConsultTeacher:
    """A Teacher that calls ``consult()`` multiple times within one
    ``teach()`` invocation — testing that the budget counts every call,
    not just rounds.
    """

    def __init__(self, *, calls_per_round: int, reply: str = "ok") -> None:
        self.calls_per_round = calls_per_round
        self.reply_text = reply
        self.seen_inputs: list[TeacherInput] = []

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        self.seen_inputs.append(inp)
        results = list(inp.prior_results)
        for i in range(self.calls_per_round):
            result = await inp.consultation.consult(
                ConsultationRequest(
                    wax_id=inp.wax_id,
                    original_message=inp.original_message,
                    teacher_question=f"multi-call {inp.round_number}-{i}",
                    now=inp.now,
                    prior_results=tuple(results),
                )
            )
            results.append(result)
            if inp.consultation.budget_exhausted:  # type: ignore[attr-defined]
                break
        return TeacherOutcome(
            reply_text=self.reply_text,
            next_question=None,
            context_used=tuple(results),
            finished=True,
        )


@pytest.mark.asyncio
async def test_teacher_multiple_consults_within_one_invocation_are_counted() -> None:
    """A Teacher that calls consult() 3 times in one teach() invocation
    uses 3 budget units — not 1. With max_consultations=3, all calls
    succeed; the Teacher finishes with 3 results.
    """
    storage = InMemoryStorage()
    teacher = _MultiConsultTeacher(calls_per_round=3)
    world = make_world(storage=storage, teacher=teacher, max_consultations=3)  # type: ignore[arg-type]
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "hello")
    assert result.tutor_entry is not None
    assert result.consultations_made == 3, (
        f"expected 3 consultations, got {result.consultations_made}"
    )


@pytest.mark.asyncio
async def test_budget_exhausted_mid_invocation_returns_empty_result() -> None:
    """If the budget is exhausted mid-invocation, further consult() calls
    return a budget_exhausted result. The Teacher can finish with the
    context it already has.
    """
    storage = InMemoryStorage()
    # Teacher tries 5 calls in round 0, but max_consultations=2.
    teacher = _MultiConsultTeacher(calls_per_round=5)
    world = make_world(storage=storage, teacher=teacher, max_consultations=2)  # type: ignore[arg-type]
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "hello")
    assert result.tutor_entry is not None
    assert result.consultations_made == 2, (
        f"budget must cap at 2; got {result.consultations_made}"
    )


@pytest.mark.asyncio
async def test_budget_is_shared_across_rounds() -> None:
    """The budget is shared across Teacher rounds. A Teacher that uses 2
    calls in round 0 has only 1 call left in round 1 (max=3).
    """
    storage = InMemoryStorage()

    class _StagedTeacher:
        def __init__(self) -> None:
            self.round = 0

        async def teach(self, inp: TeacherInput) -> TeacherOutcome:
            if self.round == 0:
                self.round += 1
                # Use 2 calls in round 0.
                results = list(inp.prior_results)
                for _ in range(2):
                    r = await inp.consultation.consult(
                        ConsultationRequest(
                            wax_id=inp.wax_id,
                            original_message=inp.original_message,
                            teacher_question="round 0 call",
                            now=inp.now,
                            prior_results=tuple(results),
                        )
                    )
                    results.append(r)
                return TeacherOutcome(
                    reply_text=None,
                    next_question="more",
                    context_used=tuple(results),
                    finished=False,
                )
            # Round 1: try 2 more calls, but only 1 remains in the budget.
            results = list(inp.prior_results)
            for _ in range(2):
                r = await inp.consultation.consult(
                    ConsultationRequest(
                        wax_id=inp.wax_id,
                        original_message=inp.original_message,
                        teacher_question="round 1 call",
                        now=inp.now,
                        prior_results=tuple(results),
                    )
                )
                results.append(r)
            return TeacherOutcome(
                reply_text="done",
                next_question=None,
                context_used=tuple(results),
                finished=True,
            )

    teacher = _StagedTeacher()
    world = make_world(storage=storage, teacher=teacher, max_consultations=3)  # type: ignore[arg-type]
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "hello")
    assert result.tutor_entry is not None
    assert result.consultations_made == 3, (
        f"budget must be shared across rounds: 2+1=3; got {result.consultations_made}"
    )


@pytest.mark.asyncio
async def test_teacher_can_respond_without_any_consultation() -> None:
    """A Teacher that never calls consult() uses 0 budget units."""
    storage = InMemoryStorage()

    class _NoCITeacher:
        async def teach(self, inp: TeacherInput) -> TeacherOutcome:
            return TeacherOutcome(
                reply_text="no CI needed",
                next_question=None,
                context_used=(),
                finished=True,
            )

    world = make_world(storage=storage, teacher=_NoCITeacher(), max_consultations=3)  # type: ignore[arg-type]
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "hello")
    assert result.tutor_entry is not None
    assert result.consultations_made == 0
