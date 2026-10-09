"""ContextInvestigator orchestration tests (mock model + fake tools)."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
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
async def test_context_intelligence_investigates() -> None:
    model = MockContextModel()
    investigator = ContextInvestigator(
        model=model,
        tools={"search_semantically": FakeSemanticTool()},
    )
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=new_wax_id(),
            conversation_id=uuid4(),
            query="Remember the project Mr A gave me?",
        )
    )
    assert model.calls >= 1
    assert package.summary
    assert len(package.evidence) >= 1
    assert package.investigated
