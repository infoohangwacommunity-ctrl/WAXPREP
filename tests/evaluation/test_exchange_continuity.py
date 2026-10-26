"""Acceptance tests for guaranteed continuity through Context Intelligence.

Two promises are held here at once.

The product promise: a student who says "that", "it", "earlier" or "you
said" gets an answer that knows what they meant. The architecture
promise: the application never decides what matters — Context
Intelligence does, and the exchange reaches the Teacher only because CI
carried it.

A pipeline where the exchange *might* arrive is not this product. These
tests are about the guarantee, not the possibility.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from support import (
    ModelTeacherWorld,
    World,
    add_student,
    make_model_teacher_world,
    make_world,
)
from waxprep.context.exchange import trim_exchange
from waxprep.context.gateway_model import GatewayContextModel
from waxprep.context.model import ContextModelFinal, ContextModelRequest
from waxprep.context.models import ExchangeTurn
from waxprep.context.system_prompt import CONTEXT_INTELLIGENCE_SYSTEM_PROMPT
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import AuthorKind
from waxprep.model_gateway.errors import ModelGatewayError
from waxprep.model_gateway.models import ModelRequest, ModelResponse, StreamEvent
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.orchestration.model_teacher_ci_first import (
    TEACHER_SYSTEM_PROMPT_CI_FIRST,
)

TUTOR_REPLY = "Let's work on it together."


class RecordingProvider(MockProvider):
    """MockProvider that keeps every request it receives."""

    def __init__(
        self,
        *,
        response_text: str = "mock response",
        response_script: list[str] | None = None,
        fail_times: int = 0,
        fail_error: ModelGatewayError | None = None,
    ) -> None:
        super().__init__(
            response_text=response_text,
            response_script=response_script,
            fail_times=fail_times,
            fail_error=fail_error,
        )
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        async for event in super().stream(request):
            yield event


async def _two_turns(world: World | ModelTeacherWorld, wax_id: WaxId) -> None:
    await world.services.turns.handle_student_message(
        wax_id, "I struggle with algebra."
    )
    await world.services.turns.handle_student_message(
        wax_id, "What if it is 2x + 5 = 12?"
    )


@pytest.mark.asyncio
async def test_the_application_supplies_the_exchange_to_ci() -> None:
    """CI is handed the raw exchange, in order, every turn."""
    world = make_world()
    wax_id = add_student(world)
    await _two_turns(world, wax_id)

    request = world.context_model.requests[-1]
    assert [t.text for t in request.exchange] == [
        "I struggle with algebra.",
        TUTOR_REPLY,
    ]


@pytest.mark.asyncio
async def test_ci_default_carries_the_exchange_in_the_package() -> None:
    """The default is include. Silence must never strip the exchange."""
    world = make_world()
    wax_id = add_student(world)
    await _two_turns(world, wax_id)

    result = await world.services.turns.handle_student_message(
        wax_id, "And if it is 3x + 5 = 12?"
    )
    package = result.context_package
    assert package is not None
    # Every prior turn of the live exchange travels: two exchanges so far.
    assert [t.text for t in package.exchange] == [
        "I struggle with algebra.",
        TUTOR_REPLY,
        "What if it is 2x + 5 = 12?",
        TUTOR_REPLY,
    ]
    assert any(s.startswith("exchange:included:") for s in package.investigated)


@pytest.mark.asyncio
async def test_ci_can_drop_the_exchange_and_the_drop_is_audited() -> None:
    """Dropping is CI's decision — and it is visible, never silent."""
    world = make_world()
    wax_id = add_student(world)
    world.context_model.final = ContextModelFinal(
        summary="Nothing personal was needed here.",
        selected_evidence_ids=(),
        uncertainty=(),
        stopped_reason="model_finalized",
        include_exchange=False,
        include_exchange_reason="thread genuinely irrelevant",
    )
    await _two_turns(world, wax_id)

    result = await world.services.turns.handle_student_message(
        wax_id, "And if it is 3x + 5 = 12?"
    )
    package = result.context_package
    assert package is not None
    assert package.exchange == ()
    assert any(s.startswith("exchange:dropped:") for s in package.investigated)
    assert any("exchange_dropped" in u for u in package.uncertainty)


@pytest.mark.asyncio
async def test_the_teacher_renders_the_exchange_before_the_current_message() -> None:
    """The Teacher reads the exchange in order, and the new message lands
    after it — so "that" has something to point at."""
    provider = RecordingProvider(response_text="Sure, let's work it out.")
    world = make_model_teacher_world(provider=provider)
    wax_id = add_student(world)
    await _two_turns(world, wax_id)

    prompt = provider.requests[-1].input[0].text
    assert prompt is not None
    assert "Student: I struggle with algebra." in prompt
    assert "Wax Prep: Sure, let's work it out." in prompt
    current = prompt.index("Student (just now):")
    assert prompt.index("Student: I struggle with algebra.") < current
    assert prompt.index("Wax Prep: Sure, let's work it out.") < current


@pytest.mark.asyncio
async def test_the_evidence_section_does_not_repeat_the_exchange() -> None:
    """The same turn read out twice is noise. The exchange is the primary
    carrier of the live thread, so the evidence section stays out of it."""
    provider = RecordingProvider(response_text="Sure, let's work it out.")
    world = make_model_teacher_world(provider=provider)
    wax_id = add_student(world)
    await _two_turns(world, wax_id)

    prompt = provider.requests[-1].input[0].text
    assert prompt is not None
    assert prompt.count("I struggle with algebra.") == 1
    assert prompt.count("Sure, let's work it out.") == 1
    assert prompt.count("What if it is 2x + 5 = 12?") == 1


@pytest.mark.asyncio
async def test_a_cold_start_carries_no_exchange() -> None:
    world = make_world()
    wax_id = add_student(world)
    result = await world.services.turns.handle_student_message(wax_id, "Hello there")
    package = result.context_package
    assert package is not None
    assert package.exchange == ()


@pytest.mark.asyncio
async def test_the_exchange_travels_even_without_a_back_reference() -> None:
    """The guarantee is not conditional on the student saying "that".

    The application must not decide that a message looks like a
    follow-up — that is the vocabulary-based judgement this architecture
    forbids. CI decides, on the whole exchange.
    """
    world = make_world()
    wax_id = add_student(world)
    await world.services.turns.handle_student_message(wax_id, "Set up: x + 5 = 12.")
    result = await world.services.turns.handle_student_message(
        wax_id, "Now solve 3x = 21."
    )
    package = result.context_package
    assert package is not None
    assert [t.text for t in package.exchange] == [
        "Set up: x + 5 = 12.",
        TUTOR_REPLY,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["What is 7 times 8?", "hello", "Thanks!", "Solve 3x = 21."],
)
async def test_every_message_shape_is_supplied_the_same_exchange(
    message: str,
) -> None:
    """No application-side filtering: the supply does not depend on what the
    message looks like."""
    world = make_world()
    wax_id = add_student(world)
    await world.services.turns.handle_student_message(wax_id, "Earlier context.")
    result = await world.services.turns.handle_student_message(wax_id, message)
    package = result.context_package
    assert package is not None
    assert [t.text for t in package.exchange] == [
        "Earlier context.",
        TUTOR_REPLY,
    ]


@pytest.mark.asyncio
async def test_the_triggering_message_is_not_repeated_in_the_exchange() -> None:
    """The Teacher renders the current message separately; the exchange
    must not duplicate it."""
    world = make_world()
    wax_id = add_student(world)
    await world.services.turns.handle_student_message(wax_id, "First")
    result = await world.services.turns.handle_student_message(wax_id, "Second")
    package = result.context_package
    assert package is not None
    assert all(t.text != "Second" for t in package.exchange)


@pytest.mark.asyncio
async def test_the_quality_event_records_whether_continuity_happened() -> None:
    """Continuity is a product property, so it lands in the quality signal
    the judge will read."""
    world = make_world()
    wax_id = add_student(world)
    await _two_turns(world, wax_id)
    await world.services.turns.handle_student_message(wax_id, "One more thing?")

    events = world.storage.list_events(wax_id)
    quality = [e for e in events if e.kind == "turn_quality"]
    assert quality, "a turn_quality event must be recorded for the turn"
    assert quality[-1].metadata["exchange_included"] is True
    assert int(quality[-1].metadata["exchange_turns"]) == 4


def test_trimming_drops_the_oldest_turns_first() -> None:
    """Length is the only reason a supplied turn is dropped, and the recent
    end survives."""
    turns = tuple(
        ExchangeTurn(author_kind=AuthorKind.STUDENT, text="x" * 10) for _ in range(4)
    )
    trimmed = trim_exchange(turns, max_characters=25)
    assert len(trimmed) == 2
    assert trimmed[-1].text == "x" * 10


def test_a_single_oversized_turn_is_kept_and_shortened() -> None:
    """A partial newest turn still beats no turn at all."""
    turns = (ExchangeTurn(author_kind=AuthorKind.STUDENT, text="y" * 100),)
    trimmed = trim_exchange(turns, max_characters=10)
    assert len(trimmed) == 1
    assert trimmed[0].text == "y" * 10


def test_ci_prompt_states_that_the_teacher_cannot_see_the_thread() -> None:
    """The prompt has to carry the instruction, or the default is only a
    convention."""
    prompt = CONTEXT_INTELLIGENCE_SYSTEM_PROMPT
    assert "include_exchange" in prompt
    assert "The Teacher cannot see the thread unless you carry it." in prompt


def test_ci_actually_sees_the_exchange_material_it_must_judge() -> None:
    """CI is asked to decide about the exchange — so it has to be shown it."""
    request = ContextModelRequest(
        system_prompt=CONTEXT_INTELLIGENCE_SYSTEM_PROMPT,
        user_query="What if it is 2x + 5 = 12?",
        evidence="",
        tools=[],
        max_output_tokens=100,
        wax_id=new_wax_id(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        exchange=(
            ExchangeTurn(author_kind=AuthorKind.STUDENT, text="Solve x + 5 = 12."),
            ExchangeTurn(author_kind=AuthorKind.TUTOR, text="Start by subtracting 5."),
        ),
    )
    prompt = GatewayContextModel._final_prompt(request)
    assert "Student: Solve x + 5 = 12." in prompt
    assert "Wax Prep: Start by subtracting 5." in prompt
    assert "include_exchange" in prompt


def test_a_cold_start_is_stated_to_ci_rather_than_left_to_infer() -> None:
    request = ContextModelRequest(
        system_prompt=CONTEXT_INTELLIGENCE_SYSTEM_PROMPT,
        user_query="Hello",
        evidence="",
        tools=[],
        max_output_tokens=100,
        wax_id=new_wax_id(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        exchange=(),
        exchange_empty=True,
    )
    prompt = GatewayContextModel._final_prompt(request)
    assert "none yet" in prompt


def test_the_teacher_prompt_promises_what_the_pipeline_keeps() -> None:
    """The Teacher is told it receives the recent exchange — and the
    pipeline really does put it there."""
    assert "the recent\nexchange" in TEACHER_SYSTEM_PROMPT_CI_FIRST
