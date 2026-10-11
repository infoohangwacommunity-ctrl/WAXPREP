"""Teacher-facing consultation adapter over Context Intelligence.

The Teacher does not call ``ContextInvestigator.investigate`` directly.
This adapter translates a ``ConsultationRequest`` (Teacher-facing) into
an ``InvestigationRequest`` (CI-internal) and translates the
``ContextPackage`` back into a ``ConsultationResult``.

This separation means:
- The Teacher never sees CI's internal model classes.
- The original student message is preserved unchanged — CI receives it
  via ``InvestigationRequest.query`` but the Teacher always holds the
  ``HistoryEntry``.
- CI cannot become a gatekeeper: the Teacher decides whether to call
  this adapter at all.
"""

from __future__ import annotations

import logging
from typing import Any

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.models import (
    ContextPackage,
    InvestigationLimits,
    InvestigationRequest,
)
from waxprep.domain.models import InvestigationRecord, new_id
from waxprep.orchestration.contracts import (
    ConsultationRequest,
    ConsultationResult,
    TeacherConsultation,
)

logger = logging.getLogger(__name__)


class CIBackedConsultation(TeacherConsultation):
    """Adapts ``ContextInvestigator`` to the Teacher-facing contract.

    Each call to ``consult`` runs one CI investigation. The result is
    recorded as a ``context_investigations`` audit row.
    """

    def __init__(
        self,
        investigator: ContextInvestigator,
        *,
        storage: Any | None = None,
        context_model_name: str = "context",
        limits: InvestigationLimits | None = None,
    ) -> None:
        self._investigator = investigator
        self._storage = storage
        self._context_model_name = context_model_name
        self._limits = limits

    async def consult(self, request: ConsultationRequest) -> ConsultationResult:
        ci_request = InvestigationRequest(
            wax_id=request.wax_id,
            query=request.teacher_question or request.original_message.text or "",
            now=request.now,
            trigger_entry_id=request.original_message.id,
            limits=self._limits or InvestigationLimits(),
        )
        try:
            package: ContextPackage = await self._investigator.investigate(ci_request)
        except Exception:
            logger.exception("context investigation failed; returning empty result")
            return ConsultationResult(
                evidence=(),
                synthesis="Context was unavailable for this consultation.",
                uncertainty=("context_unavailable",),
                provenance_refs=(),
                insufficient=True,
                stopped_reason="investigation_failed",
            )
        if self._storage is not None:
            try:
                self._storage.record_investigation(
                    InvestigationRecord(
                        id=new_id(),
                        wax_id=request.wax_id,
                        trigger_entry_id=request.original_message.id,
                        input_text=package.query,
                        result={
                            "summary": package.summary,
                            "evidence_ids": [str(e.id) for e in package.evidence],
                            "uncertainty": list(package.uncertainty),
                            "investigated": list(package.investigated),
                            "teacher_question": request.teacher_question,
                            "round": len(request.prior_results),
                        },
                        model=self._context_model_name,
                        created_at=request.now,
                    )
                )
            except Exception:
                logger.exception("could not record investigation audit")
        return ConsultationResult(
            evidence=package.evidence,
            synthesis=package.summary,
            uncertainty=package.uncertainty,
            provenance_refs=(),
            currentness={},
            insufficient=bool(package.uncertainty),
            stopped_reason=package.stopped_reason,
            metadata={"investigated": list(package.investigated)},
        )


__all__ = ["CIBackedConsultation"]
