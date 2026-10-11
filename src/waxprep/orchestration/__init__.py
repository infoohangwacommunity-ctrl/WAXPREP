"""Teacher-first orchestration package.

Public API:
- ``TurnService`` — drives a student turn: save → Teacher (bounded CI
  consultations) → save tutor reply → outbox.
- ``Teacher`` Protocol — the teaching contract.
- ``ModelTeacher`` — the real, model-backed Teacher implementation. It
  builds provider-neutral requests via the existing ``ModelGateway``,
  decides per message whether to consult Context Intelligence, evaluates
  evidence, and composes the final reply under a strictly validated
  response contract.
- ``TeacherConsultation`` Protocol — the capability the Teacher uses
  to consult Context Intelligence.
- ``BoundedConsultation`` — enforces the consultation budget at the
  capability boundary, counting every CI call across the entire turn.
- ``ConsultationRequest`` / ``ConsultationResult`` — request/response
  for one CI consultation.
- ``TeacherInput`` / ``TeacherOutcome`` — input/output for one round
  of the Teacher.
- ``MockTeacher`` / ``MockTeacherScript`` — deterministic test double.
- ``errors`` — typed orchestration failures (invalid model output,
  invalid teacher outcomes).
"""

from __future__ import annotations

from waxprep.orchestration.bounded import (
    BoundedConsultation,
    BudgetExhaustedConsultation,
)
from waxprep.orchestration.consultation import CIBackedConsultation
from waxprep.orchestration.contracts import (
    ConsultationRequest,
    ConsultationResult,
    Teacher,
    TeacherConsultation,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.orchestration.errors import (
    InvalidTeacherOutcomeError,
    OrchestrationError,
    TeacherModelError,
)
from waxprep.orchestration.mock_teacher import MockTeacher, MockTeacherScript
from waxprep.orchestration.model_teacher import (
    DEFAULT_TEACHER_MODEL_NAME,
    ModelTeacher,
)
from waxprep.orchestration.turn import TurnResult, TurnService

__all__ = [
    "BoundedConsultation",
    "BudgetExhaustedConsultation",
    "CIBackedConsultation",
    "ConsultationRequest",
    "ConsultationResult",
    "DEFAULT_TEACHER_MODEL_NAME",
    "InvalidTeacherOutcomeError",
    "MockTeacher",
    "MockTeacherScript",
    "ModelTeacher",
    "OrchestrationError",
    "Teacher",
    "TeacherConsultation",
    "TeacherInput",
    "TeacherModelError",
    "TeacherOutcome",
    "TurnResult",
    "TurnService",
]
