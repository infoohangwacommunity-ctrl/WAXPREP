"""Orchestration package.

Live WhatsApp and production composition use CI-first only.

CI-FIRST ARCHITECTURE (current — the only live path):
- ``TurnServiceCiFirst`` — save → CI (mandatory) → Teacher → save tutor reply → outbox.
- ``TeacherCiFirst`` Protocol — simplified teaching contract.
- ``ModelTeacherCiFirst`` — model-backed Teacher; receives ContextPackage, returns plain text.
- ``TurnResultCiFirst`` — result of a CI-first turn.

Teacher-first modules (``turn``, ``model_teacher``, ``contracts``,
``bounded``, ``consultation``) are quarantined. They emit
``DeprecationWarning`` on import and remain only so legacy unit tests can
still import them. Do not use them in new code or production composition.
"""

from __future__ import annotations

# CI-first architecture (current — only path used in production)
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
from waxprep.orchestration.contracts_ci_first import (
    TextDeltaSink,
)
from waxprep.orchestration.errors import (
    InvalidTeacherOutcomeError,
    OrchestrationError,
    TeacherModelError,
)
from waxprep.orchestration.mock_teacher import MockTeacher, MockTeacherScript
from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst
from waxprep.orchestration.turn_ci_first import (
    TurnResultCiFirst,
    TurnServiceCiFirst,
)

# Quarantined Teacher-first imports are deliberate and emit DeprecationWarning.
from waxprep.orchestration.bounded import (  # noqa: E402
    BoundedConsultation,
    BudgetExhaustedConsultation,
)
from waxprep.orchestration.consultation import CIBackedConsultation  # noqa: E402
from waxprep.orchestration.contracts import (  # noqa: E402
    ConsultationRequest,
    ConsultationResult,
    TeacherConsultation,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.orchestration.model_teacher import ModelTeacher  # noqa: E402
from waxprep.orchestration.turn import TurnResult, TurnService  # noqa: E402

__all__ = [
    # CI-first (current)
    "DEFAULT_TEACHER_MODEL_NAME",
    "ModelTeacherCiFirst",
    "TeacherCiFirst",
    "TeacherInputCiFirst",
    "TeacherOutcomeCiFirst",
    "TextDeltaSink",
    "TurnResultCiFirst",
    "TurnServiceCiFirst",
    "InvalidTeacherOutcomeError",
    "MockTeacher",
    "MockTeacherScript",
    "OrchestrationError",
    "TeacherModelError",
    # Quarantined Teacher-first (deprecated)
    "BoundedConsultation",
    "BudgetExhaustedConsultation",
    "CIBackedConsultation",
    "ConsultationRequest",
    "ConsultationResult",
    "ModelTeacher",
    "TeacherConsultation",
    "TeacherInput",
    "TeacherOutcome",
    "TurnResult",
    "TurnService",
]
