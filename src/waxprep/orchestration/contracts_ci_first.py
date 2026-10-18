"""CI-first orchestration contracts.

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

Key differences from Teacher-first:
- CI is mandatory for every message, not optional
- Teacher does NOT decide whether to consult CI
- Teacher does NOT generate consultation questions
- Teacher receives a complete ContextPackage, not raw CI results
- Teacher has one job: produce the final student-facing response
- No bounded consultation loop - CI runs once per turn
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from waxprep.channel.contracts import InteractiveSuggestion
from waxprep.context.models import ContextPackage
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import HistoryEntry

# Default logical model name for CI-first Teacher
DEFAULT_TEACHER_MODEL_NAME = "teacher"

# Optional sink for progressive delivery of text deltas while the model streams.
# Infrastructure only — never educational branching.
TextDeltaSink = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TeacherInput:
    """Everything the Teacher receives in CI-first architecture.

    The Teacher receives:
    - The exact student message (never rewritten)
    - A complete ContextPackage from CI
    - The current time

    The Teacher does NOT receive a consultation capability because
    CI has already completed its investigation.
    """

    wax_id: WaxId
    original_message: HistoryEntry
    context_package: ContextPackage
    now: datetime


@dataclass(frozen=True, slots=True)
class TeacherOutcome:
    """What the Teacher returns in CI-first architecture.

    The Teacher has one job: produce the final student-facing response.
    There is no concept of "finished" or "next_question" - the Teacher
    always produces the final reply in one round.

    If the Teacher has nothing to say, it returns reply_text=None.

    ``interactive`` is optional presentation only. Body teaching must always
    live in ``reply_text``; infrastructure never forces buttons.
    """

    reply_text: str | None
    interactive: InteractiveSuggestion | None = None


class Teacher(Protocol):
    """The Teacher contract for CI-first architecture.

    The Teacher is a Protocol; the production implementation is the
    model-backed ``ModelTeacherCiFirst`` in ``orchestration.model_teacher_ci_first``.

    The Teacher:
    - Receives the exact student message and ContextPackage
    - Produces the final educational response
    - Does NOT consult CI (CI already ran)
    - Does NOT decide whether CI is needed (CI is mandatory)
    """

    async def teach(
        self,
        inp: TeacherInput,
        *,
        on_text_delta: TextDeltaSink | None = None,
    ) -> TeacherOutcome: ...


__all__ = [
    "DEFAULT_TEACHER_MODEL_NAME",
    "Teacher",
    "TeacherInput",
    "TeacherOutcome",
    "TextDeltaSink",
]
