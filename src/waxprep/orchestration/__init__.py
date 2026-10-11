"""Teacher-first orchestration package.

Public API:
- ``TurnService`` — drives a student turn: save → Teacher (bounded CI
  consultations) → save tutor reply → outbox.
- ``Teacher`` Protocol — the future Teacher intelligence contract.
  Intentionally unimplemented in this build.
- ``TeacherConsultation`` Protocol — the capability the Teacher uses
  to consult Context Intelligence.
- ``ConsultationRequest`` / ``ConsultationResult`` — request/response
  for one CI consultation.
- ``TeacherInput`` / ``TeacherOutcome`` — input/output for one round
  of the Teacher.
- ``MockTeacher`` / ``MockTeacherScript`` — deterministic test double.
"""

from __future__ import annotations

from waxprep.orchestration.consultation import CIBackedConsultation
from waxprep.orchestration.contracts import (
    ConsultationRequest,
    ConsultationResult,
    Teacher,
    TeacherConsultation,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.orchestration.mock_teacher import MockTeacher, MockTeacherScript
from waxprep.orchestration.turn import TurnResult, TurnService

__all__ = [
    "CIBackedConsultation",
    "ConsultationRequest",
    "ConsultationResult",
    "MockTeacher",
    "MockTeacherScript",
    "Teacher",
    "TeacherConsultation",
    "TeacherInput",
    "TeacherOutcome",
    "TurnResult",
    "TurnService",
]
