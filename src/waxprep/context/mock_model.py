"""Deterministic Context Intelligence mock for offline tests."""

from __future__ import annotations

from dataclasses import dataclass

from waxprep.context.model import (
    ContextModelDecision,
    ContextModelFinal,
    ContextModelRequest,
)


@dataclass
class MockContextModel:
    calls: int = 0

    async def decide(self, request: ContextModelRequest) -> ContextModelDecision:
        self.calls += 1
        query = request.user_query.lower()
        if "remember" in query:
            return ContextModelDecision(
                action="search_semantically",
                arguments={"query": request.user_query, "limit": 6},
                reason="The student refers to previous information.",
            )
        if "continue" in query:
            return ContextModelDecision(
                action="search_conversation",
                arguments={"query": request.user_query, "limit": 6},
                reason="The student refers to an earlier conversation thread.",
            )
        return ContextModelDecision(
            action="stop",
            arguments={},
            reason="No additional investigation required.",
        )

    async def finalize(self, request: ContextModelRequest) -> ContextModelFinal:
        return ContextModelFinal(
            summary=(
                "The Context Intelligence investigation completed "
                "without assuming a fixed student profile."
            ),
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="mock_model_complete",
        )
