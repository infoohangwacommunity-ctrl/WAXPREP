"""ContextInvestigator orchestration (scripted mock CI + fake tools)."""

from __future__ import annotations

from datetime import UTC, datetime
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
from waxprep.context.tools import ToolInvocation, ToolResult
from waxprep.domain.identifiers import new_wax_id

NOW = datetime(2026, 6, 1, tzinfo=UTC)


class FakeSearchTool:
    name = "find_by_meaning"

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        item_id = uuid4()
        return ToolResult(
            name=self.name,
            results=(
                Evidence(
                    id=item_id,
                    wax_id=invocation.wax_id,
                    evidence_type=EvidenceType.HISTORY,
                    source_id=item_id,
                    content="The student mentioned a project given by Mr A.",
                    created_at=NOW,
                    relevance_reason="test",
                    decision=ContextDecision.INCLUDE,
                    author_kind="student",
                ),
            ),
        )


class BrokenTool:
    name = "find_exact"

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        raise RuntimeError("database hiccup")


def _request(query: str) -> InvestigationRequest:
    return InvestigationRequest(wax_id=new_wax_id(), query=query, now=NOW)


@pytest.mark.asyncio
async def test_ci_investigates_because_the_model_chose_to() -> None:
    model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_by_meaning", {"query": "prior project"}, "look"),
            ContextModelDecision("stop", {}, "enough"),
        ]
    )
    investigator = ContextInvestigator(
        model=model, tools={"find_by_meaning": FakeSearchTool()}
    )
    package = await investigator.investigate(_request("that red thing from before?"))
    assert len(package.evidence) == 1 and package.as_of == NOW


@pytest.mark.asyncio
async def test_unscripted_model_stops_for_any_message() -> None:
    model = MockContextModel()
    package = await ContextInvestigator(model=model, tools={}).investigate(
        _request("hello")
    )
    assert package.stopped_reason == "mock_model_complete" and model.calls >= 1


@pytest.mark.asyncio
async def test_a_failing_tool_is_reported_not_fatal() -> None:
    model = MockContextModel(
        decision_script=[
            ContextModelDecision("find_exact", {"query": "x"}, "try"),
            ContextModelDecision("stop", {}, "give up"),
        ]
    )
    package = await ContextInvestigator(
        model=model, tools={"find_exact": BrokenTool()}
    ).investigate(_request("anything"))
    assert any(s.startswith("tool_failed:find_exact") for s in package.investigated)


@pytest.mark.asyncio
async def test_blank_message_needs_no_context() -> None:
    package = await ContextInvestigator(model=MockContextModel(), tools={}).investigate(
        _request("   ")
    )
    assert package.stopped_reason == "empty_query"
