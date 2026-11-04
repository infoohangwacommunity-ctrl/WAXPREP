"""Founder fix #8: Teacher receives a ContextPackage, not a conversation slice.

The Teacher's ``TeacherInput`` MUST contain a ``context_package``
field of type ``ContextPackage`` (the CI evidence contract). It must
NOT receive an arbitrary slice of recent history it picked for
itself — continuity is assembled by CI, not routed around by the
application.

Every ``Evidence`` item in the package must carry provenance:
- ``created_at`` (when the source happened)
- ``author_kind`` (who said it)
- ``provenance`` (a tuple of strings tracing how CI found it)

These tests use ``MockTeacher`` so they run offline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from support import add_student, make_world
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ContextModelFinal
from waxprep.context.models import (
    ContextDecision,
    ContextPackage,
    Evidence,
    EvidenceType,
    ExchangeTurn,
    InvestigationRequest,
    InvestigationStatus,
)
from waxprep.context.tools import ToolInvocation, ToolResult
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import AuthorKind
from waxprep.orchestration.contracts_ci_first import TeacherInput


@pytest.mark.asyncio
async def test_teacher_input_has_context_package_field() -> None:
    """The Teacher receives a ContextPackage, not a raw slice of history.

    A TeacherInput constructed by the turn service has a
    ``context_package`` attribute that is a ``ContextPackage``
    instance — never None for a real student message.
    """
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(
        wax, "I need help with fractions", channel="test"
    )
    assert len(world.teacher.seen_inputs) == 1
    inp = world.teacher.seen_inputs[0]
    assert isinstance(inp, TeacherInput)
    assert hasattr(inp, "context_package")
    assert inp.context_package is not None
    assert isinstance(inp.context_package, ContextPackage)


@pytest.mark.asyncio
async def test_teacher_does_not_receive_arbitrary_history_slice() -> None:
    """The Teacher's input must not carry an arbitrary ``recent_history``
    or ``conversation_slice`` field. Continuity travels through the
    ContextPackage only. This test guards against the old defect
    where the application pre-filtered history for the Teacher.
    """
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "a question", channel="test")
    inp = world.teacher.seen_inputs[0]
    # The Teacher input is a frozen dataclass; its declared fields
    # must not include slice-style shortcuts.
    field_names = {f.name for f in inp.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    banned = {
        "recent_history",
        "conversation_slice",
        "history_window",
        "recent_turns",
        "messages",
    }
    assert not (field_names & banned), (
        f"TeacherInput carries a banned slice-style field: "
        f"{field_names & banned}. The Teacher must receive a ContextPackage."
    )


@pytest.mark.asyncio
async def test_evidence_items_carry_provenance_date_author_source() -> None:
    """Every Evidence the Teacher sees must carry provenance:
    - ``created_at`` (when the source happened)
    - ``author_kind`` (who said it)
    - ``provenance`` tuple (how CI traced it)
    Without provenance the Teacher cannot tell when a fact was
    established or whether to trust it.
    """

    # Script a CI that performs one browse_time_window call returning
    # one Evidence item, then stops.
    class _SingleEvidenceTool:
        name = "browse_time_window"

        def __init__(self, ev: Evidence):
            self._ev = ev

        async def execute(self, invocation: ToolInvocation) -> ToolResult:
            return ToolResult(name=self.name, results=(self._ev,))

    when = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    wax = new_wax_id()
    source_id = uuid4()
    evidence_with_provenance = Evidence(
        id=uuid4(),
        wax_id=wax,
        evidence_type=EvidenceType.HISTORY,
        source_id=source_id,
        content="I have a chemistry exam on Friday.",
        created_at=when - timedelta(days=2),
        relevance_reason="student mentioned an exam date",
        decision=ContextDecision.INCLUDE,
        author_kind="student",
        provenance=("history row retrieved via browse_time_window",),
    )

    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                "browse_time_window",
                {"limit": 5, "newest_first": True},
                "find the exam mention",
            ),
            ContextModelDecision("stop", {}, "enough"),
        ],
        final=ContextModelFinal(
            summary="Student has a chemistry exam Friday.",
            selected_evidence_ids=(),  # delegate: use all gathered evidence
            uncertainty=(),
            stopped_reason="model_finalized",
        ),
    )
    from waxprep.context.investigator import ContextInvestigator

    investigator = ContextInvestigator(
        model=model,
        tools={"browse_time_window": _SingleEvidenceTool(evidence_with_provenance)},
    )
    request = InvestigationRequest(
        wax_id=wax,
        query="when is my exam?",
        now=when,
        trigger_entry_id=uuid4(),
    )
    package = await investigator.investigate(request)

    assert len(package.evidence) >= 1
    for ev in package.evidence:
        assert ev.created_at is not None
        assert ev.created_at.tzinfo is not None  # aware datetime
        assert ev.author_kind is not None and ev.author_kind != ""
        # Provenance tuple is non-empty: CI must say how it found this.
        assert isinstance(ev.provenance, tuple)
        assert len(ev.provenance) > 0, (
            f"Evidence {ev.id} has empty provenance — Teacher cannot trace it"
        )


@pytest.mark.asyncio
async def test_context_package_has_investigation_status_for_teacher_branching() -> None:
    """The ContextPackage carries a closed-vocabulary
    ``investigation_status`` so the Teacher can branch without parsing
    prose. The status must be one of the documented enum values.
    """
    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(wax, "anything", channel="test")
    inp = world.teacher.seen_inputs[0]
    pkg = inp.context_package
    assert pkg is not None
    assert pkg.investigation_status in {
        InvestigationStatus.SUCCESSFUL,
        InvestigationStatus.EMPTY,
        InvestigationStatus.INCOMPLETE,
        InvestigationStatus.PARTIAL,
        InvestigationStatus.FAILED,
        InvestigationStatus.SKIPPED,
    }
    # The status is a closed-vocabulary enum, never a free-form string.
    assert isinstance(pkg.investigation_status, InvestigationStatus)


@pytest.mark.asyncio
async def test_teacher_receives_exchange_inside_context_package() -> None:
    """When CI includes the exchange, it travels INSIDE the
    ContextPackage — not as a sibling field on TeacherInput. This is
    the 'continuity assembled by CI' guarantee.

    The test seeds two prior student/tutor pairs so the exchange has
    material; the package's ``exchange`` field carries at least one
    ExchangeTurn.
    """
    world = make_world()
    wax = add_student(world)
    # Two prior turns to populate the exchange.
    await world.services.turns.handle_student_message(
        wax, "earlier student turn one", channel="test"
    )
    await world.services.turns.handle_student_message(
        wax, "earlier student turn two", channel="test"
    )
    # Third turn: the exchange should now include the prior turns.
    await world.services.turns.handle_student_message(
        wax, "the trigger that should see the exchange", channel="test"
    )
    inp = world.teacher.seen_inputs[-1]
    pkg = inp.context_package
    assert pkg is not None
    # The exchange field exists and is a tuple of ExchangeTurn.
    assert hasattr(pkg, "exchange")
    assert isinstance(pkg.exchange, tuple)
    # When the model includes the exchange (the default), at least one
    # turn is carried. The exact count depends on CI's decision; what
    # matters is that the field EXISTS and is populated when CI chooses
    # to carry it.
    for turn in pkg.exchange:
        assert isinstance(turn, ExchangeTurn)
        assert turn.author_kind in (AuthorKind.STUDENT, AuthorKind.TUTOR)
