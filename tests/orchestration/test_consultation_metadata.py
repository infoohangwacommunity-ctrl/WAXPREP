"""CIBackedConsultation preserves currentness from evidence timestamps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ContextModelFinal
from waxprep.context.models import (
    ContextDecision,
    ContextPackage,
    Evidence,
    EvidenceType,
)
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import AuthorKind, HistoryEntry, new_id
from waxprep.orchestration.consultation import (
    CIBackedConsultation,
    _currentness_from_package,
)
from waxprep.orchestration.contracts import ConsultationRequest


def test_currentness_maps_evidence_ages() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    then = now - timedelta(days=40)
    eid = new_id()
    package = ContextPackage(
        wax_id=new_wax_id(),
        query="q",
        evidence=(
            Evidence(
                id=eid,
                wax_id=new_wax_id(),
                evidence_type=EvidenceType.HISTORY,
                source_id=eid,
                content="hello",
                created_at=then,
                relevance_reason="test",
                decision=ContextDecision.INCLUDE,
            ),
        ),
        summary="s",
    )
    cur = _currentness_from_package(package, now)
    assert eid in cur
    assert "month" in cur[eid] or "week" in cur[eid] or "day" in cur[eid]


@pytest.mark.asyncio
async def test_consult_fills_currentness() -> None:
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(action="stop", arguments={}, reason="done"),
        ],
        final=ContextModelFinal(
            summary="none",
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="stop",
        ),
    )
    # Unscripted stop yields empty evidence — still structured fields present
    inv = ContextInvestigator(model=model, tools={})
    adapter = CIBackedConsultation(inv)
    wax = new_wax_id()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entry = HistoryEntry(
        id=new_id(),
        wax_id=wax,
        author_kind=AuthorKind.STUDENT,
        content_kind="text/plain",
        text="hello",
        occurred_at=now,
        recorded_at=now,
    )
    result = await adapter.consult(
        ConsultationRequest(
            wax_id=wax,
            original_message=entry,
            teacher_question="anything useful?",
            now=now,
        )
    )
    assert isinstance(result.currentness, dict)
    assert isinstance(result.provenance_refs, tuple)
