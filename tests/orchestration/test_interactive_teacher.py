"""Teacher optional interactive trailer + turn result propagation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from support import add_student, make_world
from waxprep.channel.contracts import InteractiveButtonSpec, InteractiveSuggestion
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput as TeacherInputCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherOutcome as TeacherOutcomeCiFirst,
)
from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst


class _InteractiveTeacher:
    async def teach(
        self,
        inp: TeacherInputCiFirst,
        *,
        on_text_delta: object | None = None,
    ) -> TeacherOutcomeCiFirst:
        return TeacherOutcomeCiFirst(
            reply_text="Here is a short explanation of photosynthesis.",
            interactive=InteractiveSuggestion(
                body_text="Here is a short explanation of photosynthesis.",
                buttons=(
                    InteractiveButtonSpec(title="Try a problem"),
                    InteractiveButtonSpec(title="Explain simpler"),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_turn_propagates_interactive_suggestion() -> None:
    world = make_world()
    wax_id = add_student(world)
    world.services.turns._teacher = _InteractiveTeacher()  # type: ignore[attr-defined]
    result = await world.services.turns.handle_student_message(
        wax_id,
        "What is photosynthesis?",
        channel="whatsapp",
        channel_ref="wamid.int-1",
        occurred_at=datetime.now(tz=UTC),
    )
    assert result.interactive is not None
    assert len(result.interactive.buttons) == 2
    assert result.tutor_entry is not None
    assert "photosynthesis" in (result.tutor_entry.text or "").lower()


def test_split_interactive_trailer() -> None:
    raw = (
        "Fractions are parts of a whole.\n\n"
        "<<<choices>>>\n"
        "Try a problem\n"
        "Give example\n"
        "Explain simpler\n"
    )
    body, interactive = ModelTeacherCiFirst._split_interactive(raw)
    assert body is not None
    assert "Fractions" in body
    assert "<<<choices>>>" not in body
    assert interactive is not None
    assert len(interactive.buttons) == 3
    assert interactive.buttons[0].title == "Try a problem"


def test_split_interactive_absent() -> None:
    body, interactive = ModelTeacherCiFirst._split_interactive("Just teach.")
    assert body == "Just teach."
    assert interactive is None
