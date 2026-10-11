"""Deterministic Context Intelligence mock for offline tests.

Decisions are scripted by tests. This mock must not classify student
messages by keyword lists — that would recreate the architecture defect
this foundation is designed to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from waxprep.context.model import (
    ContextModelDecision,
    ContextModelFinal,
    ContextModelRequest,
)


@dataclass
class MockContextModel:
    """Test double for Context Intelligence.

    Provide ``decision_script`` when a test needs investigation steps.
    With an empty script the model always stops (no vocabulary routing).
    ``requests`` records every request so tests can inspect what CI was shown.
    """

    calls: int = 0
    decision_script: list[ContextModelDecision] = field(
        default_factory=lambda: list[ContextModelDecision]()
    )
    final: ContextModelFinal | None = None
    requests: list[ContextModelRequest] = field(
        default_factory=lambda: list[ContextModelRequest]()
    )

    async def decide(self, request: ContextModelRequest) -> ContextModelDecision:
        self.requests.append(request)
        self.calls += 1
        if not self.decision_script:
            return ContextModelDecision(
                action="stop",
                arguments={},
                reason="Mock CI: no investigation scripted.",
            )
        index = min(self.calls - 1, len(self.decision_script) - 1)
        return self.decision_script[index]

    async def finalize(self, request: ContextModelRequest) -> ContextModelFinal:
        self.requests.append(request)
        if self.final is not None:
            return self.final
        return ContextModelFinal(
            summary=(
                "The Context Intelligence investigation completed "
                "without assuming a fixed student profile."
            ),
            selected_evidence_ids=(),
            uncertainty=(),
            stopped_reason="mock_model_complete",
        )
