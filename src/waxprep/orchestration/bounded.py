"""Budget-enforcing consultation wrapper.

Enforces the configured maximum at the consultation-capability boundary,
counting every CI call across the entire turn — not merely Teacher
rounds. A Teacher that attempts multiple consultations within one
``teach()`` invocation is counted correctly.

When the budget is exhausted, further consultations return an explicit
"budget_exhausted" result rather than calling the underlying CI. This
lets the Teacher finish the turn with whatever context it already has.
"""

from __future__ import annotations

import logging

from waxprep.orchestration.contracts import (
    ConsultationRequest,
    ConsultationResult,
    TeacherConsultation,
)

logger = logging.getLogger(__name__)


class BudgetExhaustedConsultation(TeacherConsultation):
    """Returned when the consultation budget is exhausted.

    Further calls return this result instead of calling the underlying
    CI. The Teacher receives an explicit signal that no more
    consultations are available and must finish with the context it
    already has.
    """

    @staticmethod
    def result() -> ConsultationResult:
        return ConsultationResult(
            evidence=(),
            synthesis="Consultation budget exhausted. No further CI calls available.",
            uncertainty=("budget_exhausted",),
            provenance_refs=(),
            insufficient=True,
            stopped_reason="budget_exhausted",
        )


class BoundedConsultation(TeacherConsultation):
    """Wraps a ``TeacherConsultation`` and enforces a hard budget on
    the number of ``consult()`` calls across the entire turn.

    Every call to ``consult()`` is counted — including multiple calls
    within a single ``Teacher.teach()`` invocation. When the budget is
    exhausted, further calls return a ``budget_exhausted`` result
    instead of calling the underlying CI.
    """

    def __init__(
        self,
        inner: TeacherConsultation,
        *,
        max_consultations: int,
    ) -> None:
        if max_consultations < 1:
            raise ValueError("max_consultations must be at least 1")
        self._inner = inner
        self._max = max_consultations
        self._calls = 0

    @property
    def calls_made(self) -> int:
        """Number of consultations actually made (not counting refused calls)."""
        return self._calls

    @property
    def budget_exhausted(self) -> bool:
        return self._calls >= self._max

    async def consult(self, request: ConsultationRequest) -> ConsultationResult:
        if self.budget_exhausted:
            logger.warning(
                "consultation budget exhausted (%d/%d); returning empty result",
                self._calls,
                self._max,
            )
            return BudgetExhaustedConsultation.result()
        self._calls += 1
        return await self._inner.consult(request)


__all__ = ["BoundedConsultation", "BudgetExhaustedConsultation"]
