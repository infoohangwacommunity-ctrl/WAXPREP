"""ContextInvestigator orchestration tests (scripted mock CI + fake tools)."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision
from waxprep.context.models import (
    ContextDecision,
    Evidence,
    EvidenceType,
    InvestigationRequest,
)
from waxprep.context.tools import ToolResult
from waxprep.domain.identifiers import new_wax_id


class FakeSemanticTool:
    name = "search_semantically"

    async def execute(self, wax_id: object, arguments: dict[str, Any]) -> ToolResult:
        del arguments
        evidence_id = uuid4()
        return ToolResult(
            name=self.name,
            results=(
                Evidence(
                    id=evidence_id,
                    wax_id=wax_id,  # type: ignore[arg-type]
                    evidence_type=EvidenceType.CONVERSATION,
                    source_id=evidence_id,
                    content="The student previously mentioned a project given by Mr A.",
                    created_at=datetime.now(UTC),
                    relevance_reason="Semantic test evidence.",
                    decision=ContextDecision.INCLUDE,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_context_intelligence_investigates_via_script() -> None:
    """CI investigates because the mock was scripted to — not keywords."""
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                action="search_semantically",
                arguments={"query": "prior project", "limit": 6},
                reason="Mock CI chose to investigate.",
            ),
            ContextModelDecision(
                action="stop",
                arguments={},
                reason="Enough evidence.",
            ),
        ]
    )
    investigator = ContextInvestigator(
        model=model,
        tools={"search_semantically": FakeSemanticTool()},
    )
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=new_wax_id(),
            conversation_id=uuid4(),
            query="Can we pick up that red thing from before?",
        )
    )
    assert model.calls >= 1
    assert package.summary
    assert len(package.evidence) >= 1
    assert package.investigated


@pytest.mark.asyncio
async def test_unscripted_mock_stops_without_keyword_routing() -> None:
    """Default mock stops for any non-blank message — no vocabulary path."""
    model = MockContextModel()
    investigator = ContextInvestigator(model=model, tools={})
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=new_wax_id(),
            conversation_id=uuid4(),
            query="hello",
        )
    )
    assert package.stopped_reason == "mock_model_complete"
    assert model.calls >= 1
