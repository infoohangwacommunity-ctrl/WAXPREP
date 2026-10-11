"""Deterministic mock Teacher for tests.

This is NOT teaching intelligence. The production Teacher is the
model-backed ``ModelTeacher``; this mock keeps orchestration tests
offline and reproducible while following the same ``Teacher`` Protocol.

The mock supports a small instruction script so tests can verify the
bounded consultation loop:
- ``("consult", "question")`` — ask CI a question, do not finish
- ``("reply", "text")`` — finish with this reply text
- ``("reply_without_ci", "text")`` — finish without consulting CI at all
"""

from __future__ import annotations

from dataclasses import dataclass, field

from waxprep.orchestration.contracts import (
    ConsultationRequest,
    TeacherInput,
    TeacherOutcome,
)


@dataclass
class MockTeacherScript:
    """A list of instructions the mock Teacher executes in order."""

    instructions: list[tuple[str, str]] = field(
        default_factory=lambda: [("reply_without_ci", "I understand.")]
    )


@dataclass
class MockTeacher:
    """Test double for the Teacher Protocol.

    Production uses the model-backed ``ModelTeacher``; this deterministic
    double keeps orchestration tests offline and reproducible.

    Records every input it receives so tests can assert that the exact
    original message reached the Teacher untouched.
    """

    script: MockTeacherScript = field(default_factory=MockTeacherScript)
    seen_inputs: list[TeacherInput] = field(default_factory=list)
    consultations_requested: list[str] = field(default_factory=list)

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        self.seen_inputs.append(inp)
        # Bound the rounds — if we run out of script instructions, finish
        # with whatever the last reply was, or a default.
        index = inp.round_number
        if index >= len(self.script.instructions):
            return TeacherOutcome(
                reply_text="(consultation limit reached)",
                next_question=None,
                context_used=inp.prior_results,
                finished=True,
            )
        action, payload = self.script.instructions[index]
        if action == "reply":
            return TeacherOutcome(
                reply_text=payload,
                next_question=None,
                context_used=inp.prior_results,
                finished=True,
            )
        if action == "reply_without_ci":
            return TeacherOutcome(
                reply_text=payload,
                next_question=None,
                context_used=(),
                finished=True,
            )
        if action == "consult":
            self.consultations_requested.append(payload)
            # Actually call CI.
            result = await inp.consultation.consult(
                ConsultationRequest(
                    wax_id=inp.wax_id,
                    original_message=inp.original_message,
                    teacher_question=payload,
                    now=inp.now,
                    prior_results=inp.prior_results,
                )
            )
            return TeacherOutcome(
                reply_text=None,
                next_question=payload,
                context_used=(*inp.prior_results, result),
                finished=False,
            )
        raise ValueError(f"unknown mock instruction: {action!r}")


__all__ = ["MockTeacher", "MockTeacherScript"]
