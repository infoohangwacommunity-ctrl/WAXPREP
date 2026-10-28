"""Structured tutor-quality assessments for the Quality Judge.

The Quality Judge periodically evaluates a bounded window of one
student's tutoring history and produces a structured assessment: an
overall rating, per-category ratings, strengths, actionable focus
areas, compact tutor guidance, uncertainty notes, and a confidence
level. The judge rates the *tutoring*, never the student.

Design choices grounded in the LLM-as-a-judge literature:

- Rubric-anchored categories on a fixed 1-5 scale reduce prompt
  sensitivity and score drift.
- The judge runs under its own logical model role (``ModelRole.JUDGE``)
  so operators can point it at a different model than the teacher,
  mitigating self-preference bias (judges favor their own outputs).
- Confidence is part of the output: low-confidence assessments are
  recorded as operational events but never written to the notebook, so
  one uncertain score cannot bias future tutoring.
- Assessments are advisory context, never instructions: they travel to
  the tutor as notebook evidence through Context Intelligence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from waxprep.domain.identifiers import WaxId

RATING_MIN = 1
RATING_MAX = 5
RATING_CATEGORIES: tuple[str, ...] = (
    "clarity",
    "continuity",
    "accuracy",
    "responsiveness",
    "feedback_response",
)
MAX_LIST_ITEMS = 4
MAX_UNCERTAINTY_ITEMS = 4
MAX_ITEM_CHARS = 200
MAX_GUIDANCE_CHARS = 600
MAX_TARGET_CHARS = 300


class JudgeConfidence(StrEnum):
    """How well the evaluated window supports the assessment."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class JudgeOutputError(ValueError):
    """Raised when the judge model output fails the assessment contract."""


@dataclass(frozen=True, slots=True)
class TutorQualityAssessment:
    """One structured evaluation of the tutoring quality."""

    overall: int
    categories: dict[str, int]
    strengths: tuple[str, ...]
    focus_areas: tuple[str, ...]
    tutor_guidance: str
    uncertainty: tuple[str, ...]
    confidence: JudgeConfidence
    # Separate evaluation targets (optional, bounded): what the window
    # shows about Context Intelligence serving the exchange, and about
    # the end-to-end delivery experience. Empty when the model or the
    # window supports no judgment.
    ci_review: str = ""
    delivery_review: str = ""

    def to_notebook_payload(
        self,
        *,
        as_of: str,
        trend: str,
        window_turns: int,
        window_first_id: UUID,
        window_last_id: UUID,
        previous_ratings: tuple[int, ...],
    ) -> dict[str, Any]:
        """Compact personalization shortcut written to the notebook.

        The full history of every assessment stays in the events table;
        the notebook keeps only the latest compact entry (supersession
        replaces the previous one) so future context stays small.
        """
        return {
            "kind": "tutor_quality_assessment",
            "as_of": as_of,
            "rating": self.overall,
            "confidence": self.confidence.value,
            "trend": trend,
            "categories": dict(self.categories),
            "strengths": list(self.strengths),
            "focus_areas": list(self.focus_areas),
            "tutor_guidance": self.tutor_guidance,
            "ci_review": self.ci_review,
            "delivery_review": self.delivery_review,
            "window_turns": window_turns,
            "window_first_id": str(window_first_id),
            "window_last_id": str(window_last_id),
            "previous_ratings": list(previous_ratings[-5:]),
        }


def trend_label(new_rating: int, previous_rating: int | None) -> str:
    """Relate a new rating to the previous one (bounded history)."""
    if previous_rating is None:
        return "new"
    if new_rating > previous_rating:
        return "improving"
    if new_rating < previous_rating:
        return "declining"
    return "steady"


def _clamp_list(raw: object, *, limit: int, what: str) -> tuple[str, ...]:
    """Validate a list of short strings; clamp length, never invent."""
    if not isinstance(raw, list):
        raise JudgeOutputError(f"{what} must be a list of strings")
    out: list[str] = []
    for item in raw[:limit]:
        if not isinstance(item, str) or not item.strip():
            raise JudgeOutputError(f"{what} entries must be non-empty strings")
        out.append(item.strip()[:MAX_ITEM_CHARS])
    return tuple(out)


def parse_assessment(raw: object) -> TutorQualityAssessment:
    """Strict validation of the judge model output.

    Any structural violation raises ``JudgeOutputError`` so the job
    system retries with backoff instead of persisting junk. List
    lengths are clamped (models ramble); types and ranges are strict.
    """
    if not isinstance(raw, dict):
        raise JudgeOutputError("assessment must be a JSON object")

    def _rating(value: object, what: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise JudgeOutputError(f"{what} must be an integer 1-5")
        if not RATING_MIN <= value <= RATING_MAX:
            raise JudgeOutputError(f"{what} must be an integer 1-5")
        return value

    overall = _rating(raw.get("overall"), "overall")
    categories_raw = raw.get("categories")
    if not isinstance(categories_raw, dict):
        raise JudgeOutputError("categories must be a JSON object")
    categories: dict[str, int] = {}
    for category in RATING_CATEGORIES:
        categories[category] = _rating(
            categories_raw.get(category), f"categories.{category}"
        )
    strengths = _clamp_list(
        raw.get("strengths"), limit=MAX_LIST_ITEMS, what="strengths"
    )
    focus_areas = _clamp_list(
        raw.get("focus_areas"), limit=MAX_LIST_ITEMS, what="focus_areas"
    )
    uncertainty = _clamp_list(
        raw.get("uncertainty"), limit=MAX_UNCERTAINTY_ITEMS, what="uncertainty"
    )
    guidance = raw.get("tutor_guidance")
    if not isinstance(guidance, str) or not guidance.strip():
        raise JudgeOutputError("tutor_guidance must be non-empty text")
    confidence_raw = raw.get("confidence")
    try:
        confidence = JudgeConfidence(str(confidence_raw))
    except ValueError:
        raise JudgeOutputError("confidence must be one of: low, medium, high") from None

    def _target_text(value: object, what: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise JudgeOutputError(f"{what} must be text")
        return value.strip()[:MAX_TARGET_CHARS]

    return TutorQualityAssessment(
        overall=overall,
        categories=categories,
        strengths=strengths,
        focus_areas=focus_areas,
        tutor_guidance=guidance.strip()[:MAX_GUIDANCE_CHARS],
        uncertainty=uncertainty,
        confidence=confidence,
        ci_review=_target_text(raw.get("ci_review"), "ci_review"),
        delivery_review=_target_text(raw.get("delivery_review"), "delivery_review"),
    )


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    """Everything the judge model sees for one evaluation."""

    wax_id: WaxId
    window_text: str
    window_first_id: UUID
    window_last_id: UUID
    window_turns: int
    previous_payload: dict[str, Any] | None


class JudgeModel(Protocol):
    """Judge boundary: evaluation window in, raw assessment mapping out.

    Implementations call a model and return the parsed JSON object.
    Validation happens in ``parse_assessment`` — never in the adapter —
    so every judge backend shares one contract.
    """

    async def evaluate(self, request: JudgeRequest) -> dict[str, Any]: ...


JUDGE_SYSTEM_PROMPT = """
You are the Quality Judge for WaxPrep. You periodically evaluate how
well the tutoring has been going for one student, based only on the
window of recent tutoring you are given.

You judge the TUTORING, not the student. Never rate the student's
ability, effort, behavior, background, or circumstances, and never
infer sensitive personal characteristics. When the student struggles,
that is evidence about the tutoring, not a verdict on the student.

## Rubric — rate each category 1 (very poor) to 5 (excellent)

- clarity: Are the tutor's explanations understandable and well
  structured for what this student asked?
- continuity: Does the tutor build on earlier turns in the window
  instead of starting over each time?
- accuracy: Are the tutor's answers correct and free of contradictions
  with earlier answers in the window?
- responsiveness: Does the tutor answer what the student actually
  asked, at a suitable pace and length?
- feedback_response: When the student signals confusion, correction, or
  frustration, does the tutor adjust?

## Separate targets — two short reviews, judged only from what the
## window shows

- "ci_review": Evidence in the window that context was NOT serving the
  exchange — the tutor visibly missing something the earlier turns
  contain, repeating an introduction the student already received,
  contradicting itself, or wrongly saying it lacks notes on something
  the window shows it was told. This reviews Context Intelligence's
  work, not the tutor's writing.
- "delivery_review": Evidence of a poor end-to-end experience — replies
  that arrive cut off mid-sentence, visibly repeated or duplicated
  messages, or a student left waiting or confused by delivery. This
  reviews the interaction the student actually experienced.

Both reviews must cite what the window shows. When the window supports
no judgment, use an empty string. Never speculate beyond the window.

## Output — JSON only, no markdown fences

{
  "overall": <integer 1-5>,
  "categories": {"clarity": <1-5>, "continuity": <1-5>, "accuracy": <1-5>,
                 "responsiveness": <1-5>, "feedback_response": <1-5>},
  "strengths": [<up to 4 short strings>],
  "focus_areas": [<up to 4 short, actionable strings>],
  "tutor_guidance": "<one compact paragraph for the tutor of this student:
                     what to keep doing and what to change next>",
  "ci_review": "<up to 300 characters, or empty string>",
  "delivery_review": "<up to 300 characters, or empty string>",
  "uncertainty": [<up to 4 short strings: what the window could not
                   support>],
  "confidence": "low" | "medium" | "high"
}

## Fairness rules

- Base every judgment on the window text you are given. Do not invent
  evidence and do not use outside knowledge of this student.
- "overall" should reflect the categories, weighted toward what matters
  most for this student's progress.
- Set confidence honestly: "low" when the window is short, ambiguous, or
  mostly off-topic; "high" only when the window clearly supports the
  assessment. A low-confidence assessment is still useful — it will be
  logged, but it will not steer future tutoring.
- strengths, focus_areas and tutor_guidance must be actionable for
  future turns. Never write anything that should be quoted to the
  student, and never write a judgment about the student as a person.
"""

__all__ = [
    "JUDGE_SYSTEM_PROMPT",
    "JudgeConfidence",
    "JudgeModel",
    "JudgeOutputError",
    "JudgeRequest",
    "RATING_CATEGORIES",
    "TutorQualityAssessment",
    "parse_assessment",
    "trend_label",
]
