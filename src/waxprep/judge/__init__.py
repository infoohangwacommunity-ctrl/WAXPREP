"""Quality Judge — periodic evaluation of the tutoring quality.

Public surface:
- ``TutorQualityAssessment`` / ``parse_assessment`` — the structured
  evaluation contract shared by every judge backend.
- ``JudgeModel`` — the judge boundary (window in, raw mapping out).
- ``GatewayJudgeModel`` — the production adapter over ``ModelGateway``.
- ``QualityJudgeService`` — trigger, windowing, persistence, dedup.
- ``JudgeConfig`` / ``judge_config_from_env`` — ``WAXPREP_JUDGE_*``
  knobs (``WAXPREP_JUDGE_EVERY_N_TURNS=0`` disables judging).
"""

from waxprep.judge.gateway_model import GatewayJudgeModel
from waxprep.judge.models import (
    JUDGE_SYSTEM_PROMPT,
    JudgeConfidence,
    JudgeModel,
    JudgeOutputError,
    JudgeRequest,
    TutorQualityAssessment,
    parse_assessment,
    trend_label,
)
from waxprep.judge.service import (
    EVENT_KIND_ASSESSED,
    EVENT_KIND_SCHEDULED,
    JOB_JUDGE_TUTOR_QUALITY,
    NOTEBOOK_KIND,
    JudgeConfig,
    JudgeOutcome,
    QualityJudgeService,
    judge_config_from_env,
)

__all__ = [
    "EVENT_KIND_ASSESSED",
    "EVENT_KIND_SCHEDULED",
    "JUDGE_SYSTEM_PROMPT",
    "JOB_JUDGE_TUTOR_QUALITY",
    "NOTEBOOK_KIND",
    "GatewayJudgeModel",
    "JudgeConfidence",
    "JudgeConfig",
    "JudgeModel",
    "JudgeOutcome",
    "JudgeOutputError",
    "JudgeRequest",
    "QualityJudgeService",
    "TutorQualityAssessment",
    "judge_config_from_env",
    "parse_assessment",
    "trend_label",
]
