"""Orchestration package.

Live WhatsApp path uses CI-first only; Teacher-first is deprecated.

CI-FIRST ARCHITECTURE (current, recommended):
- ``TurnServiceCiFirst`` \u2014 drives a student turn: save \u2192 CI (mandatory) \u2192 Teacher \u2192 save tutor reply \u2192 outbox.
- ``TeacherCiFirst`` Protocol \u2014 the simplified teaching contract.
- ``TeacherInputCiFirst`` \u2014 input for CI-first Teacher.
- ``TeacherOutcomeCiFirst`` \u2014 output from CI-first Teacher.
- ``ModelTeacherCiFirst`` \u2014 the real, model-backed Teacher for CI-first. It
  receives ContextPackage from CI and produces plain text output.
- ``TurnResultCiFirst`` \u2014 result of a CI-first turn.

OLD ARCHITECTURE (deprecated, still available for migration):
- ``TurnService`` \u2014 Teacher-first orchestration (deprecated).
- ``Teacher`` Protocol \u2014 old teaching contract with consultation.
- ``TeacherInput`` \u2014 old input with consultation capability.
- ``TeacherOutcome`` \u2014 old output with next_question and context_used.
- ``TeacherConsultation`` Protocol \u2014 consultation capability (deprecated).
- ``BoundedConsultation`` \u2014 budget enforcement (deprecated).
- ``ConsultationRequest`` / ``ConsultationResult`` \u2014 old request/response.
- ``MockTeacher`` / ``MockTeacherScript`` \u2014 test doubles (still useful).
- ``errors`` \u2014 typed orchestration failures.

Use CI-first for all new development. The old architecture remains for
backward compatibility during migration.
"""

from __future__ import annotations

# Old Teacher-first architecture (deprecated)
from waxprep.orchestration.bounded import (
    BoundedConsultation,
    BudgetExhaustedConsultation,
)
from waxprep.orchestration.consultation import CIBackedConsultation
from waxprep.orchestration.contracts import (
    ConsultationRequest,
    ConsultationResult,
    TeacherConsultation,
    TeacherInput,
    TeacherOutcome,
)

# CI-first architecture (current)
from waxprep.orchestration.contracts_ci_first import (
    DEFAULT_TEACHER_MODEL_NAME,
)
from waxprep.orchestration.contracts_ci_first import (
    Teacher as TeacherCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput as TeacherInputCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherOutcome as TeacherOutcomeCiFirst,
)
from waxprep.orchestration.errors import (
    InvalidTeacherOutcomeError,
    OrchestrationError,
    TeacherModelError,
)
from waxprep.orchestration.mock_teacher import MockTeacher, MockTeacherScript
from waxprep.orchestration.model_teacher import ModelTeacher
from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst
from waxprep.orchestration.turn import TurnResult, TurnService
from waxprep.orchestration.turn_ci_first import (
    TurnResultCiFirst,
    TurnServiceCiFirst,
)

__all__ = [
    # CI-first (current)
    "DEFAULT_TEACHER_MODEL_NAME",
    "ModelTeacherCiFirst",
    "TeacherCiFirst",
    "TeacherInputCiFirst",
    "TeacherOutcomeCiFirst",
    "TurnResultCiFirst",
    "TurnServiceCiFirst",
    # Old architecture (deprecated)
    "BoundedConsultation",
    "BudgetExhaustedConsultation",
    "CIBackedConsultation",
    "ConsultationRequest",
    "ConsultationResult",
    "InvalidTeacherOutcomeError",
    "MockTeacher",
    "MockTeacherScript",
    "ModelTeacher",
    "OrchestrationError",
    "TeacherConsultation",
    "TeacherInput",
    "TeacherModelError",
    "TeacherOutcome",
    "TurnResult",
    "TurnService",
]
