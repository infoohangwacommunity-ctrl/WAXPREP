"""Unit tests for the model-backed Teacher adapter.

The ModelTeacher must build provider-neutral requests through the
existing gateway, pass the exact student message unchanged, follow the
strict response contract, and fail in a controlled (typed) way on
malformed model output. No paid calls: the provider is a deterministic
mock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from waxprep.context.models import (
    ContextDecision,
    ContextPackage,
    Evidence,
    EvidenceType,
    InvestigationRequest,
)
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import AuthorKind, HistoryEntry
from waxprep.model_gateway.errors import ModelGatewayError, TimeoutError
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentType,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry
from waxprep.orchestration.bounded import BudgetExhaustedConsultation
from waxprep.orchestration.consultation import CIBackedConsultation
from waxprep.orchestration.contracts import (
    ConsultationRequest,
    ConsultationResult,
    TeacherInput,
)
from waxprep.orchestration.errors import TeacherModelError
from waxprep.orchestration.model_teacher import (
    TEACHER_SYSTEM_PROMPT,
    ModelTeacher,
)

NOW = datetime(2026, 6, 1, 10, 30, tzinfo=UTC)


def reply_json(text: str) -> str:
    return json.dumps({"action": "reply", "reply_text": text})


def consult_json(question: str) -> str:
    return json.dumps({"action": "consult", "question": question})


class RequestRecordingProvider(MockProvider):
    """MockProvider that records every request it receives."""

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


@dataclass
class RecordingConsultation:
    """TeacherConsultation double that records requests and returns a
    deterministic result."""

    result: ConsultationResult | None = None
    requests: list[ConsultationRequest] = field(default_factory=list)
    results: list[ConsultationResult] = field(default_factory=list)

    async def consult(self, request: ConsultationRequest) -> ConsultationResult:
        self.requests.append(request)
        returned = self.result or ConsultationResult(
            evidence=(),
            synthesis="No relevant context found.",
            uncertainty=("nothing_found",),
            insufficient=True,
            stopped_reason="no_evidence",
        )
        self.results.append(returned)
        return returned


def make_entry(text: str, wax_id: WaxId | None = None) -> HistoryEntry:
    wax = wax_id or new_wax_id()
    return HistoryEntry(
        id=uuid4(),
        wax_id=wax,
        author_kind=AuthorKind.STUDENT,
        content_kind="text/plain",
        text=text,
        occurred_at=NOW,
        recorded_at=NOW,
    )


def make_input(
    consultation: RecordingConsultation,
    *,
    text: str = "Please explain photosynthesis",
    round_number: int = 0,
    max_rounds: int = 3,
    prior_results: tuple[ConsultationResult, ...] = (),
) -> TeacherInput:
    entry = make_entry(text)
    return TeacherInput(
        wax_id=entry.wax_id,
        original_message=entry,
        now=NOW,
        consultation=consultation,  # type: ignore[arg-type]
        prior_results=prior_results,
        round_number=round_number,
        max_rounds=max_rounds,
    )


def make_teacher(provider: MockProvider) -> ModelTeacher:
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider="mock",
            model="mock-teacher-endpoint",
            role=ModelRole.TEACHER,
            capabilities=provider.capabilities("mock-teacher-endpoint"),
        )
    )
    return ModelTeacher(ModelGateway(registry), model_name="teacher")


async def test_builds_provider_neutral_request_with_teacher_role() -> None:
    provider = RequestRecordingProvider(response_text=reply_json("Hello!"))
    teacher = make_teacher(provider)
    inp = make_input(RecordingConsultation())
    outcome = await teacher.teach(inp)
    assert outcome.finished
    request = provider.requests[0]
    assert request.role is ModelRole.TEACHER
    assert request.model == "teacher", "the logical model name must be used"
    assert request.wax_id == inp.wax_id
    assert request.instructions == TEACHER_SYSTEM_PROMPT
    assert request.metadata["wax_id"] == str(inp.wax_id)
    assert request.metadata["round"] == "0"


async def test_exact_student_message_reaches_the_model_unchanged() -> None:
    provider = RequestRecordingProvider(response_text=reply_json("ok"))
    teacher = make_teacher(provider)
    exact = "  Hello!!  ¿Qué pasa? 我不明白  "
    inp = make_input(RecordingConsultation(), text=exact)
    await teacher.teach(inp)
    prompt = provider.requests[0].input[0].text
    assert prompt is not None and exact in prompt, (
        "the verbatim message, including whitespace, punctuation and "
        "non-English text, must reach the model"
    )


async def test_valid_final_response_produces_finished_outcome() -> None:
    provider = MockProvider(response_text=reply_json("Photosynthesis is ..."))
    teacher = make_teacher(provider)
    consultation = RecordingConsultation()
    outcome = await teacher.teach(make_input(consultation))
    assert outcome.finished is True
    assert outcome.reply_text == "Photosynthesis is ..."
    assert outcome.next_question is None
    assert outcome.context_used == ()
    assert consultation.requests == [], "no CI call was requested"


async def test_consult_decision_calls_ci_through_the_capability() -> None:
    provider = MockProvider(
        response_text=consult_json("What did the student say about their exam?")
    )
    teacher = make_teacher(provider)
    consultation = RecordingConsultation()
    inp = make_input(consultation)
    outcome = await teacher.teach(inp)

    assert outcome.finished is False
    assert outcome.next_question == "What did the student say about their exam?"
    assert outcome.reply_text is None
    assert len(consultation.requests) == 1
    request = consultation.requests[0]
    assert request.teacher_question == "What did the student say about their exam?"
    assert request.original_message is inp.original_message, (
        "the exact saved HistoryEntry must be forwarded, unchanged"
    )
    assert request.wax_id == inp.wax_id
    assert request.now == inp.now
    assert request.prior_results == ()
    assert outcome.context_used == (*inp.prior_results, consultation.results[0])


def _evidence(wax_id: WaxId) -> Evidence:
    return Evidence(
        id=uuid4(),
        wax_id=wax_id,
        evidence_type=EvidenceType.HISTORY,
        source_id=uuid4(),
        content="my exam is in three months",
        created_at=NOW,
        relevance_reason="exact words",
        decision=ContextDecision.NEEDS_CHECKING,
        author_kind="student",
        provenance=("said by the student",),
    )


async def test_prior_results_are_rendered_for_the_next_round() -> None:
    provider = RequestRecordingProvider(response_text=reply_json("Here we go."))
    teacher = make_teacher(provider)
    consultation = RecordingConsultation()
    inp = make_input(consultation)
    evidence = _evidence(inp.wax_id)
    prior = ConsultationResult(
        evidence=(evidence,),
        synthesis="The student mentioned an exam in three months.",
        uncertainty=("may_be_out_of_date",),
        stopped_reason="model_complete",
        metadata={"investigated": ["find_exact: looked for exam words"]},
    )
    inp = TeacherInput(
        wax_id=inp.wax_id,
        original_message=inp.original_message,
        now=inp.now,
        consultation=inp.consultation,
        prior_results=(prior,),
        round_number=1,
        max_rounds=3,
    )
    outcome = await teacher.teach(inp)
    assert outcome.finished
    prompt = provider.requests[0].input[0].text
    assert prompt is not None
    assert "The student mentioned an exam in three months." in prompt
    assert str(evidence.id) in prompt
    assert "my exam is in three months" in prompt
    assert "may_be_out_of_date" in prompt
    assert "said by the student" in prompt
    assert "find_exact: looked for exam words" in prompt
    assert "Investigation 1" in prompt


async def test_malformed_text_raises_typed_error() -> None:
    provider = MockProvider(response_text="I think we should look at the notebook.")
    teacher = make_teacher(provider)
    with pytest.raises(TeacherModelError):
        await teacher.teach(make_input(RecordingConsultation()))


async def test_unknown_action_raises_typed_error() -> None:
    provider = MockProvider(response_text='{"action": "dance", "reply_text": "x"}')
    teacher = make_teacher(provider)
    with pytest.raises(TeacherModelError):
        await teacher.teach(make_input(RecordingConsultation()))


async def test_missing_reply_text_raises_typed_error() -> None:
    provider = MockProvider(response_text='{"action": "reply"}')
    teacher = make_teacher(provider)
    with pytest.raises(TeacherModelError):
        await teacher.teach(make_input(RecordingConsultation()))


async def test_missing_question_raises_typed_error() -> None:
    provider = MockProvider(response_text='{"action": "consult"}')
    teacher = make_teacher(provider)
    with pytest.raises(TeacherModelError):
        await teacher.teach(make_input(RecordingConsultation()))


async def test_blank_final_text_raises_typed_error() -> None:
    provider = MockProvider(response_text='{"action": "reply", "reply_text": "  \\n"}')
    teacher = make_teacher(provider)
    with pytest.raises(TeacherModelError):
        await teacher.teach(make_input(RecordingConsultation()))


async def test_empty_model_output_raises_typed_error() -> None:
    provider = MockProvider(response_text="   ")
    teacher = make_teacher(provider)
    with pytest.raises(TeacherModelError):
        await teacher.teach(make_input(RecordingConsultation()))


async def test_gateway_failure_propagates_typed_error() -> None:
    provider = MockProvider(
        fail_times=3,
        fail_error=TimeoutError("provider timed out"),
        response_text=reply_json("never reached"),
    )
    teacher = make_teacher(provider)
    with pytest.raises(ModelGatewayError):
        await teacher.teach(make_input(RecordingConsultation()))


async def test_consult_decision_in_forced_final_round_raises() -> None:
    provider = MockProvider(response_text=consult_json("one more look?"))
    teacher = make_teacher(provider)
    inp = make_input(RecordingConsultation(), round_number=3, max_rounds=3)
    with pytest.raises(TeacherModelError):
        await teacher.teach(inp)


async def test_consult_decision_after_budget_exhausted_result_raises() -> None:
    provider = MockProvider(response_text=consult_json("one more look?"))
    teacher = make_teacher(provider)
    exhausted = BudgetExhaustedConsultation.result()
    inp = make_input(
        RecordingConsultation(),
        round_number=1,
        max_rounds=3,
        prior_results=(exhausted,),
    )
    with pytest.raises(TeacherModelError):
        await teacher.teach(inp)


async def test_budget_exhausted_result_surfaces_as_unfinished_outcome() -> None:
    """When the bounded wrapper returns the exhausted result, the Teacher
    surfaces it and leaves the finishing to the next (forced) round."""
    provider = MockProvider(response_text=consult_json("more?"))
    teacher = make_teacher(provider)

    class ExhaustingConsultation(RecordingConsultation):
        async def consult(self, request: ConsultationRequest) -> ConsultationResult:
            self.requests.append(request)
            return BudgetExhaustedConsultation.result()

    outcome = await teacher.teach(make_input(ExhaustingConsultation()))
    assert outcome.finished is False
    assert outcome.context_used[-1].stopped_reason == "budget_exhausted"
    assert outcome.context_used[-1].insufficient


async def test_teacher_does_not_route_on_message_content() -> None:
    """The decision flow must be identical for different kinds of messages:
    no application-side keyword or phrase routing exists."""
    provider = MockProvider(response_text=consult_json("investigate?"))
    teacher = make_teacher(provider)
    for text in ("good morning", "What is 2 + 2?", "I failed my exam"):
        outcome = await teacher.teach(make_input(RecordingConsultation(), text=text))
        assert outcome.finished is False, text


async def test_consultation_receives_the_exact_contract_via_ci_adapter() -> None:
    """Sanity: ModelTeacher feeds CIBackedConsultation exactly like the
    contract expects, end to end through the real adapter."""
    provider = MockProvider(response_text=consult_json("find prior exam talk"))
    teacher = make_teacher(provider)

    class _PackageInvestigator:
        async def investigate(self, request: InvestigationRequest) -> ContextPackage:
            return ContextPackage(
                wax_id=request.wax_id,
                query=request.query,
                evidence=(),
                summary="Nothing relevant was found for this question.",
                uncertainty=("nothing_found",),
                investigated=("stop: nothing to search",),
                stopped_reason="model_complete",
                as_of=request.now,
            )

    consultation = CIBackedConsultation(_PackageInvestigator())  # type: ignore[arg-type]
    inp = make_input(consultation)  # type: ignore[arg-type]
    outcome = await teacher.teach(inp)
    assert outcome.finished is False
    assert outcome.next_question == "find prior exam talk"
    result = outcome.context_used[-1]
    assert result.synthesis == "Nothing relevant was found for this question."
    assert result.insufficient


async def test_request_is_text_only_without_tools_or_media() -> None:
    """The basic Teacher path is text-in, text-out: no media inputs and no
    tool declarations are attached to the request."""
    provider = RequestRecordingProvider(response_text=reply_json("ok"))
    teacher = make_teacher(provider)
    await teacher.teach(make_input(RecordingConsultation()))
    request = provider.requests[0]
    assert all(item.type is ContentType.TEXT for item in request.input)
    assert request.tools == ()
