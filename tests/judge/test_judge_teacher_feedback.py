"""The tutor receives judge guidance as ordinary notebook evidence.

The Teacher never sees a score instruction or an internal report: the
assessment travels inside the ContextPackage as NOTEBOOK evidence the
same way every other durable understanding does. The prompt carries
its content; the evidence id does not appear; the reply is a normal
teaching reply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from support import START, make_teacher_gateway
from waxprep.context.evidence import notebook_evidence
from waxprep.context.models import ContextPackage
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import AuthorKind, HistoryEntry, NotebookEntry, new_id
from waxprep.model_gateway.models import (
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.orchestration.contracts_ci_first import TeacherInput
from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst

GUIDANCE = "Keep using worked examples; connect new topics to the last exercise."
REPLY = "Let us try another worked example together."


class RequestRecordingProvider(MockProvider):
    """MockProvider double that records every complete() and stream() call."""

    def __init__(self, *, response_text: str = REPLY) -> None:
        super().__init__(response_text=response_text)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        async for event in super().stream(request):
            yield event


def judge_payload() -> dict[str, object]:
    """A notebook payload shaped exactly like a judge assessment."""
    return {
        "kind": "tutor_quality_assessment",
        "as_of": START.isoformat(),
        "rating": 4,
        "confidence": "high",
        "trend": "improving",
        "categories": {"clarity": 4, "continuity": 5},
        "strengths": ["clear worked examples"],
        "focus_areas": ["connect new topics to earlier exercises"],
        "tutor_guidance": GUIDANCE,
        "window_turns": 4,
        "previous_ratings": [3, 4],
    }


def make_input() -> tuple[TeacherInput, NotebookEntry]:
    wax_id = new_wax_id()
    now = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    entry = NotebookEntry(
        id=new_id(),
        wax_id=wax_id,
        notebook_id=new_id(),
        schema_version=1,
        payload=judge_payload(),
        created_at=now,
        updated_at=now,
    )
    evidence = notebook_evidence(entry, "tutor-quality assessment")
    package = ContextPackage(
        wax_id=wax_id,
        query="fractions",
        evidence=(evidence,),
        summary="The tutoring has been going well; keep using worked examples.",
        stopped_reason="enough",
        as_of=now,
    )
    student_entry = HistoryEntry(
        id=new_id(),
        wax_id=wax_id,
        author_kind=AuthorKind.STUDENT,
        content_kind="text",
        text="Can you explain fractions again?",
        occurred_at=now,
        recorded_at=now,
    )
    inp = TeacherInput(
        wax_id=wax_id,
        original_message=student_entry,
        context_package=package,
        now=now,
    )
    return inp, entry


async def test_teacher_prompt_carries_judge_guidance() -> None:
    provider = RequestRecordingProvider()
    gateway = make_teacher_gateway(provider)
    teacher = ModelTeacherCiFirst(gateway)

    inp, entry = make_input()
    outcome = await teacher.teach(inp)

    assert outcome.reply_text
    assert provider.requests, "the teacher must have called the model"
    prompt = "\n".join((item.input[0].text or "") for item in provider.requests)
    # The guidance reached the tutor as plain advisory context…
    assert "worked examples" in prompt
    assert "tutor_quality_assessment" in prompt
    assert "improving" in prompt
    # …without internal plumbing: no evidence ids in the prompt.
    assert str(entry.id) not in prompt
    # The judge's own evaluation never masquerades as student words.
    assert "Student (just now):" in prompt
    assert "Can you explain fractions again?" in prompt


async def test_teacher_reply_is_ordinary_teaching() -> None:
    provider = RequestRecordingProvider()
    gateway = make_teacher_gateway(provider)
    teacher = ModelTeacherCiFirst(gateway)

    inp, _entry = make_input()
    outcome = await teacher.teach(inp)

    # The reply is whatever the model said — the judge does not inject
    # text into the student-facing channel.
    assert outcome.reply_text
    assert "tutor_quality_assessment" not in outcome.reply_text


def test_judge_payload_stays_compact() -> None:
    """The notebook shortcut is small: bounded lists, no window text."""
    payload = judge_payload()
    rendered = str(payload)
    assert len(rendered) < 600
    assert "Here is a worked example" not in rendered
