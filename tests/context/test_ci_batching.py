"""CI batched retrieval: independent lookups run concurrently, budgets hold.

The PDF architecture asks Context Intelligence to be able to select several
*independent* lookups in one decision so the runtime can execute them
concurrently. These tests pin the three halves of that contract: parsing
(the model may return an ``actions`` list), execution (a batch really is
concurrent — proven deterministically with an event, not with sleeps), and
honesty (missing tools are recorded, mechanical budgets still truncate).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from waxprep.context.gateway_model import GatewayContextModel
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ToolSelection
from waxprep.context.models import (
    ContextDecision,
    Evidence,
    EvidenceType,
    InvestigationLimits,
    InvestigationRequest,
    InvestigationStatus,
)
from waxprep.context.tools import ToolInvocation, ToolResult
from waxprep.domain.identifiers import WaxId, new_wax_id

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _evidence(wax_id: WaxId, content: str) -> Evidence:
    item_id = uuid4()
    return Evidence(
        id=item_id,
        wax_id=wax_id,
        evidence_type=EvidenceType.HISTORY,
        source_id=item_id,
        content=content,
        created_at=NOW,
        relevance_reason="test",
        decision=ContextDecision.INCLUDE,
        author_kind="student",
    )


class EchoTool:
    """A tool that always returns one piece of evidence with fixed text."""

    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self._content = content

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(
            name=self.name, results=(_evidence(invocation.wax_id, self._content),)
        )


def _request(
    query: str, limits: InvestigationLimits | None = None
) -> InvestigationRequest:
    return InvestigationRequest(
        wax_id=new_wax_id(),
        query=query,
        now=NOW,
        limits=limits if limits is not None else InvestigationLimits(),
    )


def test_parse_decision_parses_batched_actions() -> None:
    model = GatewayContextModel(None, model_name="context")  # type: ignore[arg-type]
    decision = model._parse_decision(
        '{"action":"find_by_meaning","arguments":{"query":"x"},'
        '"reason":"independent lookups","actions":['
        '{"action":"browse_time_window","arguments":{"limit":3},"reason":"recent"},'
        '{"action":"find_exact","arguments":{"query":"y"},"reason":"exact word"}]}'
    )
    assert decision.action == "find_by_meaning"
    assert [s.action for s in decision.batch] == ["browse_time_window", "find_exact"]
    assert decision.batch[0].arguments == {"limit": 3}
    assert decision.batch[1].reason == "exact word"


def test_parse_batch_skips_malformed_entries() -> None:
    model = GatewayContextModel(None, model_name="context")  # type: ignore[arg-type]
    decision = model._parse_decision(
        '{"action":"stop","arguments":{},"reason":"r","actions":['
        '"not_an_object", 5, {"reason":"no action"},'
        '{"action":"find_exact"}]}'
    )
    assert decision.action == "stop"
    assert len(decision.batch) == 1
    survivor = decision.batch[0]
    assert survivor.action == "find_exact"
    assert survivor.arguments == {}


def test_parse_decision_without_actions_has_empty_batch() -> None:
    model = GatewayContextModel(None, model_name="context")  # type: ignore[arg-type]
    decision = model._parse_decision('{"action":"stop","arguments":{},"reason":"r"}')
    assert decision.batch == ()


@pytest.mark.asyncio
async def test_batch_lookups_execute_concurrently() -> None:
    """A waiter that only finishes once a sibling releases it proves batching.

    If the runtime executed the batch sequentially the waiter would time
    out waiting on an event nothing sets until after it. With a concurrent
    gather both tools finish, and both pieces of evidence survive.
    """
    gate = asyncio.Event()

    class WaiterTool:
        name = "tool_waiter"

        async def execute(self, invocation: ToolInvocation) -> ToolResult:
            await asyncio.wait_for(gate.wait(), timeout=5)
            return ToolResult(
                name=self.name,
                results=(_evidence(invocation.wax_id, "waiter evidence"),),
            )

    class ReleaserTool:
        name = "tool_release"

        async def execute(self, invocation: ToolInvocation) -> ToolResult:
            gate.set()
            return ToolResult(
                name=self.name,
                results=(_evidence(invocation.wax_id, "releaser evidence"),),
            )

    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                "tool_waiter",
                {},
                "independent lookups selected together",
                batch=(ToolSelection("tool_release", {}, "independent sibling"),),
            ),
            ContextModelDecision("stop", {}, "enough"),
        ]
    )
    investigator = ContextInvestigator(
        model=model,
        tools={"tool_waiter": WaiterTool(), "tool_release": ReleaserTool()},
    )
    package = await investigator.investigate(_request("what did we cover before?"))
    contents = {item.content for item in package.evidence}
    assert contents == {"waiter evidence", "releaser evidence"}
    assert "batched_calls:2" in package.investigated
    assert "tool_calls:2" in package.investigated
    assert package.investigation_status is InvestigationStatus.SUCCESSFUL
    assert not any(s.startswith("tool_failed:") for s in package.investigated)


@pytest.mark.asyncio
async def test_missing_tool_inside_batch_is_recorded_and_siblings_run() -> None:
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                "find_by_meaning",
                {"query": "x"},
                "batch with one unknown tool",
                batch=(ToolSelection("ghost_tool", {}, "unknown"),),
            ),
            ContextModelDecision("stop", {}, "enough"),
        ]
    )
    investigator = ContextInvestigator(
        model=model, tools={"find_by_meaning": EchoTool("find_by_meaning", "real")}
    )
    package = await investigator.investigate(_request("anything"))
    assert "missing_tool:ghost_tool" in package.investigated
    assert "tool_calls:1" in package.investigated
    assert [item.content for item in package.evidence] == ["real"]


@pytest.mark.asyncio
async def test_batch_is_truncated_to_the_tool_budget_and_reports_incomplete() -> None:
    limits = InvestigationLimits(max_tool_calls=1)
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                "find_by_meaning",
                {"query": "x"},
                "two independent lookups",
                batch=(ToolSelection("browse_time_window", {}, "recent"),),
            ),
        ]
    )
    investigator = ContextInvestigator(
        model=model,
        tools={
            "find_by_meaning": EchoTool("find_by_meaning", "one"),
            "browse_time_window": EchoTool("browse_time_window", "two"),
        },
    )
    package = await investigator.investigate(_request("anything", limits=limits))
    assert "batched_calls:2" in package.investigated
    assert "tool_calls:1" in package.investigated
    assert "stop_reason:tool_budget_reached" in package.investigated
    assert package.investigation_status is InvestigationStatus.INCOMPLETE
    assert [item.content for item in package.evidence] == ["one"]
