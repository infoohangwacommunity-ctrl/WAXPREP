"""Deterministic mock Teacher for tests.

This is NOT teaching intelligence. The production Teacher is the
model-backed ``ModelTeacher``; this mock keeps orchestration tests
offline and reproducible while following the same ``Teacher`` Protocol.

The mock supports a small instruction script so tests can verify the
bounded consultation loop:
- ``("consult", "question")`` — ask CI a question, do not finish
- ``("reply", "text")`` — finish with this reply text
- ``("reply_without_ci", "text")`` — finish without consulting CI at all

For CI-first architecture, the mock simply returns a reply text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from waxprep.orchestration.contracts import (
    ConsultationRequest,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput as TeacherInputCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherOutcome as TeacherOutcomeCiFirst,
)

if TYPE_CHECKING:
    from typing import Protocol

    class _TeacherInputProtocol(Protocol):
        pass


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

    Supports both the old Teacher-first protocol (with consultation)
    and the new CI-first protocol (simple reply).
    """

    script: MockTeacherScript = field(default_factory=MockTeacherScript)
    seen_inputs: list[TeacherInput | TeacherInputCiFirst] = field(default_factory=list)
    consultations_requested: list[str] = field(default_factory=list)

    async def teach(
        self,
        inp: TeacherInput | TeacherInputCiFirst,
        *,
        on_text_delta: object | None = None,
    ) -> TeacherOutcome | TeacherOutcomeCiFirst:
        self.seen_inputs.append(inp)
        # Handle CI-first input (has context_package, no consultation)
        if hasattr(inp, "context_package"):
            reply = (
                self.script.instructions[0][1]
                if self.script.instructions
                else "I understand."
            )
            if on_text_delta is not None and reply:
                await on_text_delta(reply)  # type: ignore[operator]
            return TeacherOutcomeCiFirst(reply_text=reply)
        # Handle old Teacher-first input (has consultation, round_number, etc.)
        old_inp = cast(TeacherInput, inp)
        index = old_inp.round_number
        if index >= len(self.script.instructions):
            return TeacherOutcome(
                reply_text="(consultation limit reached)",
                next_question=None,
                context_used=old_inp.prior_results,
                finished=True,
            )
        action, payload = self.script.instructions[index]
        if action == "reply":
            return TeacherOutcome(
                reply_text=payload,
                next_question=None,
                context_used=old_inp.prior_results,
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
            result = await old_inp.consultation.consult(
                ConsultationRequest(
                    wax_id=old_inp.wax_id,
                    original_message=old_inp.original_message,
                    teacher_question=payload,
                    now=old_inp.now,
                    prior_results=old_inp.prior_results,
                )
            )
            return TeacherOutcome(
                reply_text=None,
                next_question=payload,
                context_used=(*old_inp.prior_results, result),
                finished=False,
            )
        raise ValueError(f"unknown mock instruction: {action!r}")


__all__ = ["MockTeacher", "MockTeacherScript"]
