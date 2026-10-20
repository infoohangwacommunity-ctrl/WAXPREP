"""Behaviour contracts for the blueprint (offline, no live API)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from support import TEST_CIPHER
from waxprep.clock import FakeClock
from waxprep.context.evolution import EvolutionService
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.models import InvestigationLimits
from waxprep.context.retrieval import Retriever
from waxprep.context.tool_impls import build_default_tools
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import Student, StudentStatus, new_id
from waxprep.history.service import HistoryService
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
)
from waxprep.orchestration.mock_teacher import MockTeacher, MockTeacherScript
from waxprep.orchestration.post_turn import PostTurnQualityRecord, PostTurnReview
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst
from waxprep.storage.memory import InMemoryStorage


@pytest.mark.asyncio
async def test_every_message_saved_and_quality_event_recorded() -> None:
    storage = InMemoryStorage()
    cipher = TEST_CIPHER
    clock = FakeClock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    wax = new_wax_id()
    storage.create_student(
        Student(wax_id=wax, status=StudentStatus.ACTIVE, created_at=clock.now())
    )
    token_index = TokenIndex(cipher)
    indexer = Indexer(storage, token_index, clock, has_embedder=False)
    history = HistoryService(storage, indexer, clock)
    retriever = Retriever(storage, token_index)
    evolution = EvolutionService(storage=storage, indexer=indexer)
    investigator = ContextInvestigator(
        model=MockContextModel(),
        tools=build_default_tools(storage, retriever, None, evolution=evolution),
    )
    teacher = MockTeacher(
        script=MockTeacherScript(
            instructions=[("reply", "Hello — I'm Wax Prep. What are you working on?")]
        )
    )
    turns = TurnServiceCiFirst(
        storage,
        history,
        investigator,
        teacher,
        clock,
        ci_limits=InvestigationLimits(max_steps=2, max_tool_calls=2),
    )
    result = await turns.handle_student_message(
        wax, "Hi", channel="telegram", channel_ref="m1"
    )
    assert result.student_entry is not None
    assert result.tutor_entry is not None
    events = storage.list_events(wax)
    assert any(e.kind == "turn_quality" for e in events)


def test_reasoning_effort_default_medium() -> None:
    req = ModelRequest(
        wax_id=uuid4(),
        role=ModelRole.TEACHER,
        model="teacher",
        instructions=None,
        input=(ContentItem(type=ContentType.TEXT, text="hi"),),
        reasoning_effort="medium",
    )
    assert req.reasoning_effort == "medium"


def test_reasoning_effort_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        ModelRequest(
            wax_id=uuid4(),
            role=ModelRole.TEACHER,
            model="teacher",
            instructions=None,
            input=(ContentItem(type=ContentType.TEXT, text="hi"),),
            reasoning_effort="ultra",
        )


def test_post_turn_quality_record_shape() -> None:
    storage = InMemoryStorage()
    wax = new_wax_id()
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    storage.create_student(
        Student(wax_id=wax, status=StudentStatus.ACTIVE, created_at=now)
    )
    PostTurnReview(storage, enable_stewardship=False).record_quality(
        PostTurnQualityRecord(
            wax_id=wax,
            student_entry_id=new_id(),
            tutor_entry_id=new_id(),
            investigation_status="successful",
            evidence_count=0,
            invalid_selection=False,
            notebook_proposal_accepted=False,
            reply_chars=12,
        ),
        now=now,
    )
    events = storage.list_events(wax)
    assert len(events) == 1
    assert events[0].kind == "turn_quality"
    assert events[0].metadata["reply_chars"] == 12
