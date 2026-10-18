"""DEPRECATED: Teacher-first ModelTeacher — use CI-first modules instead.\n\nLive WhatsApp and production composition use\n`turn_ci_first.TurnServiceCiFirst` and\n`model_teacher_ci_first.ModelTeacherCiFirst` only.\nThis module remains only for legacy tests until Package E removal."""

from __future__ import annotations

import warnings

warnings.warn(
    "waxprep.orchestration.model_teacher.ModelTeacher is deprecated; use "
    "model_teacher_ci_first.ModelTeacherCiFirst. Teacher-first is quarantined.",
    DeprecationWarning,
    stacklevel=2,
)


import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from waxprep.context.evidence import format_for_model
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from waxprep.orchestration.contracts import (
    ConsultationRequest,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.orchestration.errors import TeacherModelError

logger = logging.getLogger(__name__)

# Logical model name in the registry (mirrors the "context" convention of
# GatewayContextModel). Registered with ModelRole.TEACHER at the composition
# root; see docs/ARCHITECTURE.md for the wiring and configuration.
DEFAULT_TEACHER_MODEL_NAME = "teacher"

# Status constant set by BoundedConsultation when the per-turn consultation
# budget is gone. The Teacher treats it as a clear "no more consultations"
# signal and finishes with the evidence it already has.
_BUDGET_EXHAUSTED = "budget_exhausted"

# Per-investigation render bound for prior CI results (mirrors the CI
# investigation context bound). Prior results are already compact — CI
# clips each evidence item and caps the evidence count — this only bounds
# the render of any single investigation block.
_MAX_RESULT_CHARACTERS = 12000

_ACTION_REPLY = "reply"
_ACTION_CONSULT = "consult"


TEACHER_SYSTEM_PROMPT = """
You are the Wax Prep Teacher. You teach the student directly. You are the
only part of the system that talks to the student.

How you work:
- The student's exact message is given to you verbatim. Reason from it first.
- Answer directly when the message is self-contained. Many messages need no
  personal context at all.
- Investigation is a capability, not a routine. Request a Context
  Intelligence investigation when personal or contextual evidence about
  this student would genuinely improve the answer — for example prior
  history, notebook notes, summaries, relationships, or files — and you
  are uncertain without it. Do not call CI for every message; answer
  directly when the message is self-contained.
- When you investigate, ask one specific, free-form question about exactly
  what you need to know. A real question, not a topic label.
- Use the results of earlier investigations in this turn. Do not repeat an
  investigation you already made unless you have a concrete reason.
- Evidence always carries the time it happened, how long ago that was, who
  said it, where it came from, and whether it was replaced. Treat it
  carefully:
  - What the student said is a student statement.
  - What the tutor said only shows what the tutor said — never proof of a
    fact about the student.
  - Notebook entries, summaries and relationships are derived
    interpretations with sources. Trace claims back to their sources when
    accuracy matters.
  - Old evidence may be out of date. Say so when it might be.
- Never invent facts about the student. If evidence is missing or
  insufficient, be honest about what you do not know and answer as well as
  you can without it.
- The investigation summary is evidence for you, not the answer. You write
  the final educational response yourself.
- Adapt naturally to the student's demonstrated language and level from the
  actual message and evidence. Never assume facts that are not present.

Response contract (strict):
Return exactly one JSON object and nothing else — no prose before or after.
- To finish the turn:
  {"action": "reply", "reply_text": "<your complete student-facing answer>"}
- To request one investigation:
  {"action": "consult", "question": "<what Context Intelligence should investigate>"}
- "reply_text" must be a non-empty educational response addressed to the
  student.
- "question" must be a specific free-form investigation request.
- When told that no consultations remain, you MUST return a "reply".
""".strip()


@dataclass(frozen=True, slots=True)
class TeacherDecision:
    """One parsed model decision (validated shape of the model's JSON)."""

    action: str
    reply_text: str | None = None
    question: str | None = None


class ModelTeacher:
    """Production Teacher backed by a logical model via ``ModelGateway``.

    One ``teach()`` round makes one gateway call. If the model decides to
    investigate, the adapter performs exactly one consultation through the
    provided ``TeacherConsultation`` capability and returns an unfinished
    outcome; the orchestrator calls ``teach()`` again with the result in
    ``prior_results``. Every actual CI call is counted by the bounded
    consultation wrapper owned by the orchestrator.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_name: str = DEFAULT_TEACHER_MODEL_NAME,
        max_output_tokens: int | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._gateway = gateway
        self._model_name = model_name
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        can_consult = self._can_consult(inp)
        decision = await self._decide(inp, can_consult)
        if decision.action == _ACTION_CONSULT:
            question = decision.question
            if question is None:  # pragma: no cover - parse guarantees it
                raise TeacherModelError("consult decision is missing a question")
            result = await inp.consultation.consult(
                ConsultationRequest(
                    wax_id=inp.wax_id,
                    original_message=inp.original_message,
                    teacher_question=question,
                    now=inp.now,
                    prior_results=inp.prior_results,
                )
            )
            return TeacherOutcome(
                reply_text=None,
                next_question=question,
                context_used=(*inp.prior_results, result),
                finished=False,
            )
        reply_text = decision.reply_text
        if reply_text is None:  # pragma: no cover - parse guarantees it
            raise TeacherModelError("final decision is missing reply text")
        return TeacherOutcome(
            reply_text=reply_text,
            next_question=None,
            context_used=inp.prior_results,
            finished=True,
        )

    # --- model round ------------------------------------------------------------

    async def _decide(self, inp: TeacherInput, can_consult: bool) -> TeacherDecision:
        request = ModelRequest(
            wax_id=inp.wax_id,
            role=ModelRole.TEACHER,
            model=self._model_name,
            instructions=TEACHER_SYSTEM_PROMPT,
            input=(
                ContentItem(
                    type=ContentType.TEXT, text=self._build_prompt(inp, can_consult)
                ),
            ),
            metadata={
                "wax_id": str(inp.wax_id),
                "round": str(inp.round_number),
            },
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
        )
        response: ModelResponse = await self._gateway.complete(request)
        decision = self._parse_decision(response.text)
        if decision.action == _ACTION_CONSULT and not can_consult:
            logger.warning(
                "teacher model requested a consultation when none remain; "
                "failing the turn"
            )
            raise TeacherModelError(
                "model requested a consultation after consultations were exhausted"
            )
        return decision

    @staticmethod
    def _parse_decision(text: str) -> TeacherDecision:
        """Strictly validate the model's structured decision."""
        if not text or not text.strip():
            raise TeacherModelError("model returned no usable text")
        data = _extract_json_object(text)
        if data is None:
            raise TeacherModelError("model decision was not a JSON object")
        action = data.get("action")
        if action == _ACTION_REPLY:
            reply_text = data.get("reply_text")
            if not isinstance(reply_text, str) or not reply_text.strip():
                raise TeacherModelError("final decision has no non-blank reply_text")
            return TeacherDecision(action=_ACTION_REPLY, reply_text=reply_text)
        if action == _ACTION_CONSULT:
            question = data.get("question")
            if not isinstance(question, str) or not question.strip():
                raise TeacherModelError("consult decision has no non-blank question")
            return TeacherDecision(action=_ACTION_CONSULT, question=question)
        raise TeacherModelError("model decision used an unknown action value")

    # --- prompt construction ------------------------------------------------------

    def _build_prompt(self, inp: TeacherInput, can_consult: bool) -> str:
        parts: list[str] = []
        parts.append(f"Current time (UTC): {inp.now.isoformat()}")
        parts.append(f"Turn round: {inp.round_number} of {inp.max_rounds}")
        if can_consult:
            parts.append(
                "Context Intelligence consultations: available. You may request "
                "one investigation this round if you need it."
            )
        else:
            parts.append(
                "Context Intelligence consultations: exhausted. You MUST finish "
                "this turn now with the final reply, using the evidence you "
                "already have."
            )
        message = inp.original_message.text or ""
        parts.append(
            "Student message (copied exactly as received; treat it verbatim):\n"
            f"<student_message>\n{message}\n</student_message>"
        )
        if inp.prior_results:
            parts.append(self._render_prior_results(inp))
        parts.append(
            "Respond now with exactly one JSON object:\n"
            '{"action": "reply", "reply_text": "..."} to finish, or\n'
            '{"action": "consult", "question": "..."} to request one '
            "investigation."
        )
        return "\n\n".join(parts)

    def _render_prior_results(self, inp: TeacherInput) -> str:
        """Render prior CI results with evidence, provenance and uncertainty."""
        blocks: list[str] = []
        for index, result in enumerate(inp.prior_results, start=1):
            lines = [f"--- Investigation {index} ---"]
            lines.append(f"Synthesis: {result.synthesis}")
            if result.uncertainty:
                lines.append(f"Uncertainty: {', '.join(result.uncertainty)}")
            if result.stopped_reason:
                lines.append(f"Stopped reason: {result.stopped_reason}")
            investigated = result.metadata.get("investigated")
            if isinstance(investigated, list) and investigated:
                steps = "; ".join(str(step) for step in investigated)
                lines.append(f"Steps taken: {steps}")
            evidence_text = format_for_model(
                list(result.evidence), inp.now, _MAX_RESULT_CHARACTERS
            )
            lines.append(f"Evidence:\n{evidence_text}")
            blocks.append("\n".join(lines))
        header = (
            "Earlier investigations in this turn. The evidence below was "
            "gathered by Context Intelligence for you — it is material to "
            "evaluate, not the answer itself:"
        )
        return header + "\n\n" + "\n\n".join(blocks)

    # --- consultation availability ---------------------------------------------------

    @staticmethod
    def _can_consult(inp: TeacherInput) -> bool:
        """Whether a consultation is still possible in this round.

        The forced final round (round_number == max_rounds) and any prior
        budget-exhausted signal mean the Teacher must finish with the
        evidence it already has.
        """
        if inp.round_number >= inp.max_rounds:
            return False
        for result in inp.prior_results:
            if result.stopped_reason == _BUDGET_EXHAUSTED:
                return False
            if _BUDGET_EXHAUSTED in result.uncertainty:
                return False
        return True


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract one JSON object from model text.

    Follows the same tolerant-extraction convention as the Context
    Intelligence gateway model: a direct parse first, then a scan for the
    outermost braces when the model wrapped the object in prose.
    """
    stripped = text.strip()
    # Strip common markdown fences the model may wrap around JSON.
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


__all__ = ["DEFAULT_TEACHER_MODEL_NAME", "ModelTeacher", "TeacherDecision"]
