"""Direct tutor-guidance delivery: judge notebook → TeacherInput → prompt.

The notebook keeps the compact assessment for CI retrieval, but CI
retrieval is not guaranteed on every turn. The accepted guidance must
therefore also reach the Teacher directly: bounded text with its
provenance and freshness, carried on TeacherInput by the turn path —
while the event stream stays operational-only (no model text in event
metadata) and low-confidence assessments never become guidance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from support import World, add_student, make_teacher_gateway, make_world
from waxprep.context.models import ContextPackage
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import AuthorKind, HistoryEntry, new_id
from waxprep.judge.models import JudgeRequest, parse_assessment
from waxprep.judge.service import NOTEBOOK_KIND, JudgeConfig, QualityJudgeService
from waxprep.model_gateway.models import (
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.orchestration.contracts_ci_first import TeacherInput, TutorGuidance
from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst

GUIDANCE = "Keep using worked examples; connect new topics to the last exercise."
REPLY = "Let us try another worked example together."


def assessment_dict(
    *,
    rating: int = 4,
    confidence: str = "high",
    tutor_guidance: str = GUIDANCE,
    ci_review: str | None = None,
    delivery_review: str | None = None,
) -> dict[str, Any]:
    """One valid judge output (target reviews optional)."""
    data: dict[str, Any] = {
        "overall": rating,
        "categories": {
            "clarity": rating,
            "continuity": rating,
            "accuracy": rating,
            "responsiveness": rating,
            "feedback_response": rating,
        },
        "strengths": ["clear worked examples"],
        "focus_areas": ["connect new topics to earlier exercises"],
        "tutor_guidance": tutor_guidance,
        "uncertainty": ["short window"],
        "confidence": confidence,
    }
    if ci_review is not None:
        data["ci_review"] = ci_review
    if delivery_review is not None:
        data["delivery_review"] = delivery_review
    return data


class FakeJudgeModel:
    """Deterministic judge model double."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    async def evaluate(self, request: JudgeRequest) -> dict[str, Any]:
        self.calls += 1
        return self.response


def make_judge(world: World, model: FakeJudgeModel) -> QualityJudgeService:
    return QualityJudgeService(
        storage=world.storage,
        evolution=world.services.evolution,
        clock=world.clock,
        model=model,
        config=JudgeConfig(every_n_turns=2, min_pairs=2),
    )


def add_pair(world: World, wax_id: WaxId, index: int) -> None:
    """Append one completed student->tutor pair to history."""
    now = world.clock.now()
    student = HistoryEntry(
        id=new_id(),
        wax_id=wax_id,
        author_kind=AuthorKind.STUDENT,
        content_kind="text/plain",
        text=f"Explain example {index} again please.",
        occurred_at=now,
        recorded_at=now,
    )
    world.storage.append_history_entry(student)
    world.clock.advance(30)
    reply_now = world.clock.now()
    world.storage.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.TUTOR,
            content_kind="text/plain",
            text=f"Here is a worked example for step {index}.",
            occurred_at=reply_now,
            recorded_at=reply_now,
            reply_to_id=student.id,
        )
    )
    world.clock.advance(30)


def active_assessment(world: World, wax_id: WaxId) -> dict[str, Any] | None:
    notebook = world.storage.get_notebook(wax_id)
    if notebook is None:
        return None
    for entry in reversed(world.storage.list_notebook_entries(wax_id, notebook.id)):
        if entry.payload.get("kind") == NOTEBOOK_KIND:
            return dict(entry.payload)
    return None


# --- judge: accepted guidance is readable directly -------------------------


async def test_written_assessment_becomes_current_guidance() -> None:
    world = make_world()
    wax_id = add_student(world)
    judge = make_judge(world, FakeJudgeModel(assessment_dict()))
    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "written"

    guidance = judge.current_tutor_guidance(wax_id)
    assert guidance is not None
    assert "worked examples" in guidance.text
    assert guidance.confidence == "high"
    assert guidance.source == "quality_judge"
    assert guidance.assessed_at == world.clock.now()


async def test_low_confidence_never_becomes_guidance() -> None:
    world = make_world()
    wax_id = add_student(world)
    judge = make_judge(
        world, FakeJudgeModel(assessment_dict(rating=2, confidence="low"))
    )
    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "skipped_low_confidence"

    # The notebook stays clean, so no guidance is readable.
    assert judge.current_tutor_guidance(wax_id) is None


async def test_newest_guidance_wins_after_supersession() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel(assessment_dict())
    judge = make_judge(world, model)
    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    assert (await judge.evaluate_recent(wax_id)).status == "written"

    model.response = assessment_dict(
        tutor_guidance="Lead more; ask fewer permission questions."
    )
    add_pair(world, wax_id, 3)
    add_pair(world, wax_id, 4)
    assert (await judge.evaluate_recent(wax_id)).status == "written"

    guidance = judge.current_tutor_guidance(wax_id)
    assert guidance is not None
    assert "Lead more" in guidance.text


async def test_guidance_lookup_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = make_world()
    wax_id = add_student(world)
    judge = make_judge(world, FakeJudgeModel(assessment_dict()))

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("storage down")

    monkeypatch.setattr(world.storage, "get_notebook", boom)
    assert judge.current_tutor_guidance(wax_id) is None


# --- judge: separate targets reach the notebook payload ---------------------


def test_parse_target_reviews_optional_and_bounded() -> None:
    data = assessment_dict(
        ci_review="Tutor kept re-introducing itself; context likely missing.",
        delivery_review="One reply appears cut off mid-sentence.",
    )
    parsed = parse_assessment(data)
    assert "context likely missing" in parsed.ci_review
    assert "cut off" in parsed.delivery_review

    bare = parse_assessment(assessment_dict())
    assert bare.ci_review == ""
    assert bare.delivery_review == ""

    clamped = parse_assessment(assessment_dict(ci_review="x" * 500))
    assert len(clamped.ci_review) == 300


def test_notebook_payload_carries_target_reviews() -> None:
    parsed = parse_assessment(
        assessment_dict(ci_review="missed earlier turns", delivery_review="cut off")
    )
    payload = parsed.to_notebook_payload(
        as_of="2026-01-01T09:00:00+00:00",
        trend="new",
        window_turns=2,
        window_first_id=uuid4(),
        window_last_id=uuid4(),
        previous_ratings=(),
    )
    assert payload["ci_review"] == "missed earlier turns"
    assert payload["delivery_review"] == "cut off"


async def test_target_reviews_reach_notebook_end_to_end() -> None:
    world = make_world()
    wax_id = add_student(world)
    judge = make_judge(
        world,
        FakeJudgeModel(
            assessment_dict(
                ci_review="Tutor re-introduced itself twice.",
                delivery_review="One reply arrived cut off mid-sentence.",
            )
        ),
    )
    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    assert (await judge.evaluate_recent(wax_id)).status == "written"

    payload = active_assessment(world, wax_id)
    assert payload is not None
    assert payload["ci_review"] == "Tutor re-introduced itself twice."
    assert "cut off" in payload["delivery_review"]


# --- turn path: guidance reaches the Teacher on every turn ------------------


async def test_turn_path_hands_guidance_to_the_teacher() -> None:
    world = make_world()
    wax_id = add_student(world)
    judge = make_judge(world, FakeJudgeModel(assessment_dict()))
    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    assert (await judge.evaluate_recent(wax_id)).status == "written"

    turns = TurnServiceCiFirst(
        world.storage,
        world.services.history,
        world.services.investigator,
        world.teacher,
        world.clock,
        judge=judge,
    )
    await turns.handle_student_message(
        wax_id, "What is photosynthesis?", channel="test", channel_ref="g-1"
    )

    seen = world.teacher.seen_inputs[-1]
    assert seen.tutor_guidance is not None
    assert "worked examples" in seen.tutor_guidance.text


# --- teacher: guidance renders with provenance and freshness ----------------


class RequestRecordingProvider(MockProvider):
    """Records every request the Teacher makes."""

    def __init__(self) -> None:
        super().__init__(response_text=REPLY)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        async for event in super().stream(request):
            yield event


def guidance_input(now: datetime, assessed_at: datetime | None) -> TeacherInput:
    wax_id = new_wax_id()
    return TeacherInput(
        wax_id=wax_id,
        original_message=HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.STUDENT,
            content_kind="text/plain",
            text="Can you explain fractions again?",
            occurred_at=now,
            recorded_at=now,
        ),
        context_package=ContextPackage(
            wax_id=wax_id,
            query="fractions",
            evidence=(),
            summary="",
            stopped_reason="enough",
            as_of=now,
        ),
        now=now,
        tutor_guidance=(
            TutorGuidance(
                text="Lead more; ask fewer permission questions.",
                confidence="medium",
                assessed_at=assessed_at,
            )
            if assessed_at is not None
            else None
        ),
    )


async def test_teacher_prompt_renders_guidance_with_freshness() -> None:
    provider = RequestRecordingProvider()
    teacher = ModelTeacherCiFirst(make_teacher_gateway(provider))
    now = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    assessed = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    outcome = await teacher.teach(guidance_input(now, assessed))

    assert outcome.reply_text
    prompt = provider.requests[0].input[0].text or ""
    assert "Lead more; ask fewer permission questions." in prompt
    assert "confidence: medium" in prompt
    assert "assessed 1 day(s) ago" in prompt
    assert "not rules" in prompt


async def test_teacher_prompt_without_guidance_has_no_guidance_block() -> None:
    provider = RequestRecordingProvider()
    teacher = ModelTeacherCiFirst(make_teacher_gateway(provider))
    now = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)

    outcome = await teacher.teach(guidance_input(now, None))

    assert outcome.reply_text
    prompt = provider.requests[0].input[0].text or ""
    assert "Tutor quality guidance" not in prompt
