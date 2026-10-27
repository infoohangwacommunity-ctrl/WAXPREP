"""Quality Judge service: trigger, windowing, persistence, fairness.

The judge is exercised end-to-end against the real InMemory storage,
the real EvolutionService, and a deterministic judge model double.
No keyword matching anywhere: windows are selected purely by turns
after the last assessment cursor.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest
from support import TEST_CIPHER, World, add_student, make_world
from waxprep.derive.worker import DerivationWorker
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    JobStatus,
    ObjectType,
    new_id,
)
from waxprep.index.tokens import TokenIndex
from waxprep.judge.models import (
    JudgeConfidence,
    JudgeOutputError,
    JudgeRequest,
    TutorQualityAssessment,
    parse_assessment,
    trend_label,
)
from waxprep.judge.service import (
    EVENT_KIND_ASSESSED,
    EVENT_KIND_SCHEDULED,
    JOB_JUDGE_TUTOR_QUALITY,
    NOTEBOOK_KIND,
    JudgeConfig,
    QualityJudgeService,
    judge_config_from_env,
)
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst


def assessment_dict(
    *,
    rating: int = 4,
    confidence: str = "high",
    tutor_guidance: str = (
        "Keep using worked examples; connect new topics to the last exercise."
    ),
) -> dict[str, Any]:
    """One valid judge output."""
    return {
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


class FakeJudgeModel:
    """Deterministic judge model double (counts calls, can fail or lie)."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        raw: object = None,
        fail: bool = False,
    ) -> None:
        self.response = response if response is not None else assessment_dict()
        self.raw = raw
        self.fail = fail
        self.calls = 0
        self.requests: list[JudgeRequest] = []

    async def evaluate(self, request: JudgeRequest) -> dict[str, Any]:
        self.calls += 1
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("judge model unavailable")
        if self.raw is not None:
            return cast(dict[str, Any], self.raw)
        return self.response


def add_pair(
    world: World, wax_id: UUID, index: int, *, student_text: str | None = None
) -> tuple[HistoryEntry, HistoryEntry]:
    """Append one completed student->tutor pair to history."""
    now = world.clock.now()
    student = world.storage.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.STUDENT,
            content_kind="text",
            text=student_text or f"Explain example {index} again please.",
            occurred_at=now,
            recorded_at=now,
            channel="test",
            channel_ref=f"student-{wax_id}-{index}",
        )
    )
    world.clock.advance(30)
    reply_now = world.clock.now()
    tutor = world.storage.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.TUTOR,
            content_kind="text",
            text=f"Here is a worked example for step {index}.",
            occurred_at=reply_now,
            recorded_at=reply_now,
            channel="test",
            channel_ref=f"tutor-{wax_id}-{index}",
            reply_to_id=student.id,
        )
    )
    world.clock.advance(30)
    return student, tutor


def make_judge(
    world: World,
    model: FakeJudgeModel,
    *,
    every_n_turns: int = 2,
    min_pairs: int = 2,
    window_entries: int = 24,
    max_window_chars: int = 6000,
) -> QualityJudgeService:
    return QualityJudgeService(
        storage=world.storage,
        evolution=world.services.evolution,
        clock=world.clock,
        model=model,
        config=JudgeConfig(
            every_n_turns=every_n_turns,
            min_pairs=min_pairs,
            window_entries=window_entries,
            max_window_chars=max_window_chars,
        ),
    )


def judge_jobs(world: World, wax_id: UUID) -> list[tuple[str, UUID]]:
    """Claim due judge jobs (test doubles never run the worker here)."""
    jobs = world.storage.claim_due_jobs(wax_id, world.clock.now(), 50)
    return [(j.kind, j.object_id) for j in jobs if j.kind == JOB_JUDGE_TUTOR_QUALITY]


def active_assessment(world: World, wax_id: UUID) -> dict[str, Any] | None:
    notebook = world.storage.get_notebook(wax_id)
    if notebook is None:
        return None
    for entry in reversed(
        world.storage.list_notebook_entries(wax_id, notebook.id)
    ):
        if entry.payload.get("kind") == NOTEBOOK_KIND:
            return dict(entry.payload)
    return None


def assessed_events(world: World, wax_id: UUID) -> list[dict[str, Any]]:
    return [
        dict(event.metadata)
        for event in world.storage.list_events(wax_id)
        if event.kind == EVENT_KIND_ASSESSED
    ]


# --- trigger ----------------------------------------------------------------


async def test_trigger_fires_at_n_pairs_not_before() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is False
    assert judge_jobs(world, wax_id) == []

    add_pair(world, wax_id, 2)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is True
    jobs = judge_jobs(world, wax_id)
    assert len(jobs) == 1
    assert jobs[0][0] == JOB_JUDGE_TUTOR_QUALITY


async def test_no_duplicate_scheduling_for_same_window() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is True
    # The worker has not run yet: the same window must not queue twice.
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is False
    assert len(judge_jobs(world, wax_id)) == 1


async def test_disabled_config_never_schedules() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=0)

    for i in range(1, 6):
        add_pair(world, wax_id, i)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is False
    assert judge_jobs(world, wax_id) == []
    assert assessed_events(world, wax_id) == []


# --- evaluation and persistence -------------------------------------------


async def test_evaluate_writes_event_notebook_and_provenance() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel(assessment_dict(rating=4))
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    _, tutor2 = add_pair(world, wax_id, 2)

    outcome = await judge.evaluate_recent(wax_id)

    assert outcome.status == "written"
    assert outcome.model_called is True
    assert outcome.rating == 4
    assert outcome.notebook_entry_id is not None

    events = assessed_events(world, wax_id)
    assert len(events) == 1
    assert events[0]["status"] == "written"
    assert events[0]["rating"] == 4
    assert events[0]["confidence"] == "high"
    assert events[0]["window_turns"] == 2

    payload = active_assessment(world, wax_id)
    assert payload is not None
    assert payload["kind"] == NOTEBOOK_KIND
    assert payload["rating"] == 4
    assert payload["confidence"] == "high"
    assert payload["trend"] == "new"
    assert payload["window_turns"] == 2
    assert "worked examples" in payload["tutor_guidance"]

    # The event cursor points at the evaluated window end.
    cursor_events = [
        e
        for e in world.storage.list_events(wax_id)
        if e.kind == EVENT_KIND_ASSESSED
    ]
    assert cursor_events[-1].object_id == tutor2.id

    # Provenance links the notebook entry back to the evaluated turns.
    links = world.storage.list_provenance(
        wax_id, ObjectType.NOTEBOOK, outcome.notebook_entry_id
    )
    assert links
    assert all(link.source_type is ObjectType.HISTORY for link in links)


async def test_second_cycle_supersedes_and_tracks_trend() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel(assessment_dict(rating=4))
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    first = await judge.evaluate_recent(wax_id)
    assert first.status == "written"
    first_entry_id = first.notebook_entry_id
    assert first_entry_id is not None

    model.response = assessment_dict(rating=5)
    add_pair(world, wax_id, 3)
    add_pair(world, wax_id, 4)
    second = await judge.evaluate_recent(wax_id)
    assert second.status == "written"
    second_entry_id = second.notebook_entry_id
    assert second_entry_id is not None
    assert second_entry_id != first_entry_id

    # The old entry is superseded — never deleted — and points forward.
    old = world.storage.get_notebook_entry(wax_id, first_entry_id)
    assert old is not None
    assert old.status.value == "superseded"
    assert old.superseded_by_id == second_entry_id

    payload = active_assessment(world, wax_id)
    assert payload is not None
    assert payload["rating"] == 5
    assert payload["trend"] == "improving"
    assert payload["previous_ratings"] == [4, 5]

    # Only one active assessment exists; the full history is in events.
    notebook = world.storage.get_notebook(wax_id)
    assert notebook is not None
    active = [
        e
        for e in world.storage.list_notebook_entries(wax_id, notebook.id)
        if e.payload.get("kind") == NOTEBOOK_KIND
    ]
    assert len(active) == 1
    assert len(assessed_events(world, wax_id)) == 2


async def test_insufficient_window_is_cheap_and_advances_cursor() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    # Trigger eagerly, but require more pairs than one window contains.
    judge = make_judge(world, model, every_n_turns=1, min_pairs=3)

    add_pair(world, wax_id, 1)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is True

    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "insufficient_window"
    assert outcome.model_called is False
    assert model.calls == 0
    assert active_assessment(world, wax_id) is None
    events = assessed_events(world, wax_id)
    assert len(events) == 1
    assert events[0]["status"] == "insufficient_window"

    # The cursor advanced: a new thin window is not the same window.
    add_pair(world, wax_id, 2)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is True
    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "insufficient_window"


async def test_low_confidence_is_logged_but_never_biases_notebook() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel(assessment_dict(rating=2, confidence="low"))
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)

    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "skipped_low_confidence"
    assert outcome.model_called is True
    assert outcome.notebook_entry_id is None

    # The event log keeps the full durable history of assessments.
    events = assessed_events(world, wax_id)
    assert len(events) == 1
    assert events[0]["rating"] == 2
    assert events[0]["confidence"] == "low"

    # The notebook stays clean: one uncertain score must not steer it.
    assert active_assessment(world, wax_id) is None

    # A later confident assessment still works and starts fresh.
    model.response = assessment_dict(rating=4, confidence="medium")
    add_pair(world, wax_id, 3)
    add_pair(world, wax_id, 4)
    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "written"
    payload = active_assessment(world, wax_id)
    assert payload is not None
    assert payload["trend"] == "new"
    assert payload["previous_ratings"] == [4]


async def test_invalid_output_raises_for_job_retry() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel(raw={"overall": "excellent"})
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)

    # A contract violation propagates so the job system retries with
    # backoff; nothing was persisted.
    with pytest.raises(JudgeOutputError):
        await judge.evaluate_recent(wax_id)
    assert assessed_events(world, wax_id) == []
    assert active_assessment(world, wax_id) is None


async def test_retry_after_crash_before_event_write_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker retry after a partial write must not re-run the model.

    Crash point: the notebook entry was written but the cursor event
    was not. The queued job retries; the duplicate guard recognizes the
    assessment that already covers this exact window end, records the
    missing event, and skips the model call.
    """
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)

    def swallow_record(*args: Any, **kwargs: Any) -> None:
        return None

    real_record = judge._record
    monkeypatch.setattr(judge, "_record", swallow_record)
    first = await judge.evaluate_recent(wax_id)
    assert first.status == "written"
    assert assessed_events(world, wax_id) == []  # event write was lost

    monkeypatch.setattr(judge, "_record", real_record)
    model.calls = 0
    second = await judge.evaluate_recent(wax_id)
    assert second.status == "already_assessed"
    assert second.model_called is False
    assert model.calls == 0
    payload = active_assessment(world, wax_id)
    assert payload is not None
    # Exactly one active entry and one assessed event: no duplicates.
    assert len(assessed_events(world, wax_id)) == 1
    notebook = world.storage.get_notebook(wax_id)
    assert notebook is not None
    entries = [
        e
        for e in world.storage.list_notebook_entries(wax_id, notebook.id)
        if e.payload.get("kind") == NOTEBOOK_KIND
    ]
    assert len(entries) == 1


# --- worker and turn-path integration ---------------------------------------


async def test_worker_dispatches_judge_job() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is True

    worker = DerivationWorker(
        world.storage,
        TokenIndex(TEST_CIPHER),
        world.clock,
        judge=judge,
    )
    report = await worker.run_once()
    assert report.done >= 1
    assert report.failed == 0
    assert active_assessment(world, wax_id) is not None

    # The queued job is consumed.
    due = world.storage.claim_due_jobs(wax_id, world.clock.now(), 50)
    assert all(job.kind != JOB_JUDGE_TUTOR_QUALITY for job in due)


async def test_turn_path_schedules_judge_after_n_turns() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)

    turns = TurnServiceCiFirst(
        world.storage,
        world.services.history,
        world.services.investigator,
        world.teacher,
        world.clock,
        judge=judge,
    )

    await turns.handle_student_message(
        wax_id, "What is photosynthesis?", channel="test", channel_ref="turn-1"
    )
    assert judge_jobs(world, wax_id) == []

    await turns.handle_student_message(
        wax_id, "And respiration?", channel="test", channel_ref="turn-2"
    )
    jobs = judge_jobs(world, wax_id)
    assert len(jobs) == 1
    assert jobs[0][0] == JOB_JUDGE_TUTOR_QUALITY

    # Running the evaluation through the service closes the loop.
    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "written"
    assert model.calls == 1


# --- configuration and contract --------------------------------------------


def test_judge_config_from_env_parsing() -> None:
    cfg = judge_config_from_env(
        {
            "WAXPREP_JUDGE_EVERY_N_TURNS": "3",
            "WAXPREP_JUDGE_MIN_PAIRS": "1",
            "WAXPREP_JUDGE_WINDOW_ENTRIES": "10",
            "WAXPREP_JUDGE_MAX_WINDOW_CHARS": "500",
        }
    )
    assert cfg.every_n_turns == 3
    assert cfg.min_pairs == 1
    assert cfg.window_entries == 10
    assert cfg.max_window_chars == 500
    assert cfg.enabled is True

    disabled = judge_config_from_env({"WAXPREP_JUDGE_EVERY_N_TURNS": "0"})
    assert disabled.enabled is False

    defaults = judge_config_from_env({})
    assert defaults.every_n_turns == 8
    assert defaults.min_pairs == 2


def test_parse_assessment_validation() -> None:
    good = parse_assessment(assessment_dict(rating=3, confidence="medium"))
    assert isinstance(good, TutorQualityAssessment)
    assert good.overall == 3
    assert good.confidence is JudgeConfidence.MEDIUM
    assert good.categories["clarity"] == 3

    with pytest.raises(JudgeOutputError):
        parse_assessment("not an object")
    with pytest.raises(JudgeOutputError):
        parse_assessment({"overall": 6, "categories": {}})
    with pytest.raises(JudgeOutputError):
        parse_assessment(
            {
                "overall": 4,
                "categories": {
                    "clarity": 4,
                    "continuity": 4,
                    "accuracy": 4,
                    "responsiveness": 4,
                    "feedback_response": 4,
                },
                "strengths": [],
                "focus_areas": [],
                "tutor_guidance": "",
                "uncertainty": [],
                "confidence": "certain",
            }
        )
    # Missing category is a contract violation.
    with pytest.raises(JudgeOutputError):
        parse_assessment(
            {
                "overall": 4,
                "categories": {"clarity": 4},
                "strengths": [],
                "focus_areas": [],
                "tutor_guidance": "fine",
                "uncertainty": [],
                "confidence": "high",
            }
        )


def test_trend_label_cases() -> None:
    assert trend_label(4, None) == "new"
    assert trend_label(5, 4) == "improving"
    assert trend_label(3, 4) == "declining"
    assert trend_label(4, 4) == "steady"


def test_scheduling_event_uses_only_allowed_metadata() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    assert judge.maybe_schedule(wax_id, now=world.clock.now()) is True

    scheduled = [
        e
        for e in world.storage.list_events(wax_id)
        if e.kind == EVENT_KIND_SCHEDULED
    ]
    assert len(scheduled) == 1
    # Every metadata key is in the operational allowlist.
    from waxprep.security.event_metadata import validate_event_metadata

    validate_event_metadata(dict(scheduled[0].metadata))


def test_window_text_renders_pairs_verbatim() -> None:
    world = make_world()
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)

    add_pair(world, wax_id, 1, student_text="I am confused about valves.")
    add_pair(world, wax_id, 2)
    await judge.evaluate_recent(wax_id)

    request = model.requests[0]
    assert "student: I am confused about valves." in request.window_text
    assert "tutor: Here is a worked example for step 1." in request.window_text
    assert request.window_turns == 2
    assert request.previous_payload is None
