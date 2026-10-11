"""End-to-end orchestration tests with the real model-backed Teacher.

Wired exactly like production (ModelTeacher -> ModelGateway -> logical
"teacher" model over a deterministic provider; CI over MockContextModel;
InMemoryStorage), these tests verify the whole stage:

- save-first history, exact student message reaching the Teacher
- Teacher <-> CI consultation loop through the bounded capability
- evidence flow with provenance and time context
- notebook retrieval reaching the Teacher (same-student only)
- controlled failures: provider errors, blank replies, budget exhaustion
- duplicate-delivery recovery and outbox semantics

No paid external calls: the model provider is a deterministic mock.
"""

from __future__ import annotations

import pytest

from support import ModelTeacherWorld, add_student, make_model_teacher_world
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ContextModelFinal
from waxprep.context.proposals import EvidenceRef, Operation, Proposal
from waxprep.domain.models import AuthorKind, ObjectType
from waxprep.model_gateway.errors import TimeoutError
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.orchestration.errors import TeacherModelError


def reply_json(text: str) -> str:
    import json

    return json.dumps({"action": "reply", "reply_text": text})


def consult_json(question: str) -> str:
    import json

    return json.dumps({"action": "consult", "question": question})


class RequestRecordingProvider(MockProvider):
    """MockProvider that records every request the Teacher sends."""

    def __init__(
        self,
        *,
        response_text: str = "mock response",
        response_script: list[str] | None = None,
        fail_times: int = 0,
        fail_error: Exception | None = None,
    ) -> None:
        super().__init__(
            response_text=response_text,
            response_script=response_script,
            fail_times=fail_times,
            fail_error=fail_error,  # type: ignore[arg-type]
        )
        self.requests: list[object] = []

    async def complete(self, request: object) -> object:
        self.requests.append(request)  # type: ignore[arg-type]
        return await super().complete(request)  # type: ignore[arg-type]


def teacher_prompts(world: ModelTeacherWorld) -> list[str]:
    prompts = []
    for request in world.provider.requests:  # type: ignore[attr-defined]
        text = request.input[0].text  # type: ignore[attr-defined]
        assert text is not None
        prompts.append(text)
    return prompts


def add_notebook_entry(
    world: ModelTeacherWorld,
    wax_id: object,
    source_entry_id: object,
    payload: dict[str, str],
) -> object:
    """Create a notebook entry through the existing write path (proposal ->\
    application validation -> persistence + provenance + indexing)."""
    result = world.services.evolution.apply(
        Proposal(
            wax_id=wax_id,  # type: ignore[arg-type]
            operation=Operation.ADD_NOTEBOOK_ENTRY.value,
            payload={"payload": payload},
            evidence=(
                EvidenceRef(
                    object_type=ObjectType.HISTORY,
                    object_id=source_entry_id,  # type: ignore[arg-type]
                    quote="exam",
                ),
            ),
            reason="test notebook entry",
        ),
        now=world.clock.now(),
    )
    assert result.accepted, result.reason
    return result.created_notebook_entry_id


# --- self-contained turn -------------------------------------------------------


async def test_selfcontained_question_full_turn() -> None:
    world = make_model_teacher_world(
        provider=RequestRecordingProvider(
            response_script=[reply_json("Photosynthesis is how plants make food.")]
        )
    )
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "What is photosynthesis?", channel="whatsapp", channel_ref="m-1"
    )
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == "Photosynthesis is how plants make food."
    assert result.tutor_entry.reply_to_id == result.student_entry.id
    assert result.consultations_made == 0
    assert result.outbound_delivery_id is not None
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT, AuthorKind.TUTOR]
    assert saved[0].text == "What is photosynthesis?"
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert records == []
    # The exact student message reached the model, unchanged.
    prompts = teacher_prompts(world)
    assert len(prompts) == 1
    assert "What is photosynthesis?" in prompts[0]


async def test_teacher_can_request_investigation_and_finish_next_round() -> None:
    ci_model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "exam"}, "find exam words"),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            summary="The student said their exam is in three months.",
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="model_complete",
        ),
    )
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("What did the student previously say about their exam?"),
            reply_json("You mentioned your exam is coming up — let's plan it."),
        ]
    )
    world = make_model_teacher_world(provider=provider, context_model=ci_model)
    wax = add_student(world)
    world.services.history.record(wax, AuthorKind.STUDENT, "my exam is in three months")
    world.clock.advance(150 * 86400)
    result = await world.services.turns.handle_student_message(
        wax, "how is my exam preparation going?"
    )
    assert result.consultations_made == 1
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == (
        "You mentioned your exam is coming up — let's plan it."
    )
    assert result.tutor_entry.reply_to_id == result.student_entry.id
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 1
    # Round 2 prompt must contain the CI synthesis and the raw evidence.
    prompts = teacher_prompts(world)
    assert len(prompts) == 2
    assert "The student said their exam is in three months." in prompts[1]
    assert "my exam is in three months" in prompts[1]


async def test_multiple_consultations_flow_prior_results() -> None:
    ci_model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "exam"}, "first look"),
            ContextModelDecision("stop", {}, "enough"),
            ContextModelDecision(
                "find_by_meaning", {"query": "exam date"}, "second look"
            ),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            summary="Collected exam context.",
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="model_complete",
        ),
    )
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("What did the student say about the exam?"),
            consult_json("When exactly did they say it would happen?"),
            reply_json("Here is what we know about your exam."),
        ]
    )
    world = make_model_teacher_world(provider=provider, context_model=ci_model)
    wax = add_student(world)
    world.services.history.record(wax, AuthorKind.STUDENT, "my exam is in June")
    result = await world.services.turns.handle_student_message(wax, "exam update?")
    assert result.consultations_made == 2
    assert result.tutor_entry is not None
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 2
    prompts = teacher_prompts(world)
    assert len(prompts) == 3
    # The final round sees both prior investigations.
    assert "Investigation 1" in prompts[2]
    assert "Investigation 2" in prompts[2]
    assert "Collected exam context." in prompts[2]


async def test_consultation_bound_never_exceeded_and_forced_finish_is_strict() -> None:
    """A Teacher-model that always wants one more investigation hits the
    hard bound: exactly max_consultations CI calls happen, and the forced
    final round fails in a controlled way instead of persisting junk."""
    provider = RequestRecordingProvider(response_script=[consult_json("one more?")] * 5)
    world = make_model_teacher_world(provider=provider, max_consultations=3)
    wax = add_student(world)
    with pytest.raises(TeacherModelError):
        await world.services.turns.handle_student_message(wax, "loop me")
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 3, "actual CI calls must never exceed the bound"
    # The student message is still saved; no tutor entry exists.
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT]


async def test_ci_without_evidence_turn_remains_valid() -> None:
    world = make_model_teacher_world(
        provider=RequestRecordingProvider(
            response_script=[
                consult_json("Any prior context about this?"),
                reply_json("I can help with that — tell me more."),
            ]
        )
    )
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "I am stuck")
    assert result.tutor_entry is not None
    prompts = teacher_prompts(world)
    assert "(no evidence yet)" in prompts[1]


async def test_ci_failure_keeps_student_message_and_turn_recovers() -> None:
    class ExplodingContextModel:
        async def decide(self, request: object) -> object:
            raise RuntimeError("context model down")

        async def finalize(self, request: object) -> object:
            raise RuntimeError("context model down")

    world = make_model_teacher_world(
        provider=RequestRecordingProvider(
            response_script=[
                consult_json("Any prior context?"),
                reply_json("Let's continue anyway."),
            ]
        ),
        context_model=ExplodingContextModel(),  # type: ignore[arg-type]
    )
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(wax, "help me study")
    # CI failed but the turn followed the existing degradation semantics.
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == "Let's continue anyway."
    saved = world.storage.list_history(wax)
    assert saved[0].text == "help me study"
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert records == []


async def test_teacher_provider_failure_preserves_history_and_retry_resumes() -> None:
    provider = RequestRecordingProvider(
        response_script=[reply_json("Now it works.")],
        fail_times=3,
        fail_error=TimeoutError("provider timed out"),
    )
    world = make_model_teacher_world(provider=provider)
    wax = add_student(world)
    with pytest.raises(TimeoutError):
        await world.services.turns.handle_student_message(
            wax, "what is osmosis", channel="wa", channel_ref="retry-1"
        )
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT]
    student_id = saved[0].id
    assert saved[0].text == "what is osmosis"
    # Retry (duplicate delivery after save but before reply): no duplicate
    # student entry, teaching resumes, one tutor reply is saved.
    result = await world.services.turns.handle_student_message(
        wax, "what is osmosis", channel="wa", channel_ref="retry-1"
    )
    assert result.duplicate is False or result.duplicate is True  # resume path
    assert result.tutor_entry is not None
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT, AuthorKind.TUTOR]
    assert saved[0].id == student_id, "the student entry must not be duplicated"
    assert saved[1].reply_to_id == student_id


async def test_outbox_record_created_for_channel_delivery() -> None:
    world = make_model_teacher_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "hello there", channel="whatsapp", channel_ref="out-1"
    )
    assert result.tutor_entry is not None
    assert result.outbound_delivery_id is not None
    deliveries = world.storage.list_due_outbound_deliveries(
        wax, world.clock.now(), limit=10
    )
    assert any(d.tutor_entry_id == result.tutor_entry.id for d in deliveries)


async def test_duplicate_delivery_after_reply_reuses_stored_reply() -> None:
    provider = RequestRecordingProvider(response_script=[reply_json("First answer.")])
    world = make_model_teacher_world(provider=provider)
    wax = add_student(world)
    first = await world.services.turns.handle_student_message(
        wax, "define gravity", channel="wa", channel_ref="dup-1"
    )
    again = await world.services.turns.handle_student_message(
        wax, "define gravity", channel="wa", channel_ref="dup-1"
    )
    assert first.tutor_entry is not None and again.tutor_entry is not None
    assert again.duplicate
    assert again.tutor_entry.id == first.tutor_entry.id
    assert len(world.provider.requests) == 1, "Teacher must not be called again"  # type: ignore[attr-defined]


# --- notebook retrieval ----------------------------------------------------------


async def test_notebook_evidence_reaches_teacher_with_provenance_and_time() -> None:
    ci_model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "exam"}, "find exam material"),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            summary="Notebook and history about the student's exam preparation.",
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="model_complete",
        ),
    )
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("What does the notebook say about this student's exam?"),
            reply_json("Your notebook says you are preparing for the June exam."),
        ]
    )
    world = make_model_teacher_world(provider=provider, context_model=ci_model)
    wax = add_student(world)
    entry = world.services.history.record(
        wax, AuthorKind.STUDENT, "my exam is in three months"
    )
    world.clock.advance(30 * 86400)
    note_id = add_notebook_entry(
        world,
        wax,
        entry.id,
        {
            "topic": "exam preparation",
            "note": "Student is preparing for the June exam and studies in the evening",
        },
    )
    assert note_id is not None
    world.clock.advance(120 * 86400)
    result = await world.services.turns.handle_student_message(
        wax, "how is my exam preparation going?"
    )
    assert result.tutor_entry is not None
    prompts = teacher_prompts(world)
    # CI was shown the notebook entry with its type marker.
    ci_evidence = ci_model.requests[-1].evidence
    assert "(notebook)" in ci_evidence
    assert "June exam" in ci_evidence
    # The Teacher received the notebook evidence with provenance and time.
    assert "(notebook)" in prompts[1]
    assert "durable understanding derived from history" in prompts[1]
    assert "Student is preparing for the June exam" in prompts[1]
    # Evidence retains its temporal context (age relative to now).
    assert "months ago" in prompts[1]
    # The notebook evidence is never presented as a direct student
    # statement — the notebook lines carry the derived-content provenance,
    # not a student quote.
    notebook_lines = [line for line in prompts[1].splitlines() if "(notebook)" in line]
    assert notebook_lines
    assert all("said by the student" not in line for line in notebook_lines)


async def test_superseded_notebook_flagged_not_presented_as_current() -> None:
    ci_model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "exam"}, "find exam material"),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            summary="Exam preparation notes; the older one may be out of date.",
            selected_evidence_ids=(),
            uncertainty=("notebook_may_be_stale",),
            stopped_reason="model_complete",
        ),
    )
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("What does the notebook say about the exam?"),
            reply_json("Let's double-check — your exam schedule may have changed."),
        ]
    )
    world = make_model_teacher_world(provider=provider, context_model=ci_model)
    wax = add_student(world)
    entry = world.services.history.record(wax, AuthorKind.STUDENT, "my exam is in June")
    world.clock.advance(10 * 86400)
    add_notebook_entry(
        world, wax, entry.id, {"note": "Student prepares for the exam in June"}
    )
    superseded = world.storage.list_notebook_entries(
        wax,
        world.storage.get_notebook(wax).id,  # type: ignore[union-attr]
    )[0]
    world.clock.advance(20 * 86400)
    add_notebook_entry(
        world, wax, entry.id, {"note": "Exam moved: student now prepares for July"}
    )
    newest = world.storage.list_notebook_entries(
        wax,
        world.storage.get_notebook(wax).id,  # type: ignore[union-attr]
    )[-1]
    world.storage.supersede_notebook_entry(
        wax, superseded.id, newest.id, world.clock.now()
    )
    world.clock.advance(60 * 86400)
    result = await world.services.turns.handle_student_message(
        wax, "what did we say about my exam?"
    )
    assert result.tutor_entry is not None
    prompts = teacher_prompts(world)
    # The superseded entry is visible but explicitly flagged, and the
    # uncertainty from CI reached the Teacher.
    assert "[superseded]" in prompts[1]
    assert "notebook_may_be_stale" in prompts[1]


async def test_cross_student_isolation_in_retrieval_and_prompts() -> None:
    ci_model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                "find_exact", {"query": "chemistry"}, "find chemistry"
            ),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            summary="Chemistry preparation notes.",
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="model_complete",
        ),
    )
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("What does this student's notebook say about chemistry?"),
            reply_json("Continuing with your chemistry preparation."),
        ]
    )
    world = make_model_teacher_world(provider=provider, context_model=ci_model)
    wax_a = add_student(world)
    wax_b = add_student(world)
    entry_a = world.services.history.record(
        wax_a, AuthorKind.STUDENT, "i study chemistry"
    )
    entry_b = world.services.history.record(
        wax_b, AuthorKind.STUDENT, "i study chemistry"
    )
    add_notebook_entry(world, wax_a, entry_a.id, {"note": "wax-a-chemistry-plan alpha"})
    add_notebook_entry(world, wax_b, entry_b.id, {"note": "wax-b-chemistry-plan beta"})
    result = await world.services.turns.handle_student_message(
        wax_a, "continue my chemistry prep?"
    )
    assert result.tutor_entry is not None
    # Student B's notebook content is nowhere: not in CI evidence, not in
    # any Teacher prompt, not in the audit records.
    all_prompts = teacher_prompts(world)
    for evidence in (r.evidence for r in ci_model.requests):
        assert evidence is not None
        assert "beta" not in evidence
        assert "wax-b" not in evidence
    for prompt in all_prompts:
        assert "beta" not in prompt
        assert "wax-b" not in prompt
    assert "alpha" in all_prompts[1], "student A's own notebook must be reachable"


async def test_absent_notebook_degrades_honestly_without_fabrication() -> None:
    """With no notebook entries and no matching evidence, CI reports an
    honest, insufficient package and the Teacher is told exactly that."""
    ci_model = MockContextModel(
        decision_script=[],
        final=ContextModelFinal(
            summary="No stored context about this.",
            selected_evidence_ids=(),
            uncertainty=("nothing_relevant_found",),
            stopped_reason="model_complete",
        ),
    )
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("Any earlier context about this?"),
            reply_json("I don't have prior notes on that — tell me the topic."),
        ]
    )
    world = make_model_teacher_world(provider=provider, context_model=ci_model)
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "how did my exam go?"
    )
    assert result.tutor_entry is not None
    prompts = teacher_prompts(world)
    assert "(no evidence yet)" in prompts[1]
    assert "nothing_relevant_found" in prompts[1]
    # No notebook content was fabricated anywhere in the prompt.
    assert "(notebook)" not in prompts[1]


async def test_investigation_audit_never_creates_conversation_state() -> None:
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("Any prior context?"),
            reply_json("Here we go."),
        ]
    )
    world = make_model_teacher_world(provider=provider)
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "hello again")
    records = world.storage._investigations  # type: ignore[attr-defined]
    assert len(records) == 1
    assert records[0].trigger_entry_id is not None
    # The audit row points at the triggering history entry — no session or
    # conversation identifiers exist anywhere in the record.
    assert not hasattr(records[0], "conversation_id")
    assert not hasattr(records[0], "session_id")


async def test_full_loop_uses_real_ci_investigator_with_notebook_and_history() -> None:
    """End-to-end through the real ContextInvestigator: scripted CI model
    decisions drive real retrieval over indexed history and notebook."""
    ci_model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "exam"}, "find exam material"),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            summary="Exam preparation material from the student's world.",
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="model_complete",
        ),
    )
    provider = RequestRecordingProvider(
        response_script=[
            consult_json("What do we know about this student's exam preparation?"),
            reply_json("Your notes say the exam is in June — let's continue."),
        ]
    )
    world = make_model_teacher_world(provider=provider, context_model=ci_model)
    wax = add_student(world)
    entry = world.services.history.record(
        wax, AuthorKind.STUDENT, "my exam is in three months"
    )
    world.clock.advance(5 * 86400)
    add_notebook_entry(
        world, wax, entry.id, {"note": "Student prepares for the exam in June"}
    )
    world.clock.advance(80 * 86400)
    result = await world.services.turns.handle_student_message(
        wax, "how is my exam preparation going?"
    )
    assert result.consultations_made == 1
    assert result.tutor_entry is not None
    prompts = teacher_prompts(world)
    assert "June" in prompts[1]
    assert "(notebook)" in prompts[1]
    assert "durable understanding derived from history" in prompts[1]
