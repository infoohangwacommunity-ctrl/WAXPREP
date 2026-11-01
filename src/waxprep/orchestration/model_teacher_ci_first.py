"""The real, model-backed Teacher for CI-first architecture.

Implements the ``Teacher`` Protocol on top of the provider-neutral
``ModelGateway`` and the logical model registry. The Teacher owns teaching:
it interprets the current student message with the ContextPackage, and
composes the final educational answer.

Responsibility boundaries preserved:
- The exact student message reaches the model unchanged.
- ContextPackage is input to the Teacher, never the student-facing answer.
- The Teacher never writes to history or storage; persistence stays with
  the ``TurnServiceCiFirst`` and storage layer.
- No keyword routing, phrase lists, or student-specific branches live here.
  Every decision is made by the model from the actual message and evidence;
  this adapter only enforces the response contract and provider-neutral
  request construction.

Response contract (validated strictly):
The Teacher MUST return plain text (the final student-facing answer).
No JSON parsing is required - the model returns prose directly.
"""

from __future__ import annotations

import logging
from datetime import datetime

from waxprep.context.evidence import format_for_model
from waxprep.context.models import ContextPackage
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from waxprep.orchestration.contracts_ci_first import TeacherInput, TeacherOutcome

logger = logging.getLogger(__name__)

# Logical model name in the registry (mirrors the "context" convention of
# GatewayContextModel). Registered with ModelRole.TEACHER at the composition
# root; see docs/ARCHITECTURE.md for the wiring and configuration.
DEFAULT_TEACHER_MODEL_NAME = "teacher"

# Maximum characters for rendering context package evidence to the model.
_MAX_CONTEXT_RENDER_CHARACTERS = 12000


TEACHER_SYSTEM_PROMPT_CI_FIRST = """
You are Wax — a patient, warm, and relatable peer tutor for students on mobile messaging channels.

HOW YOU RELATE TO THE STUDENT:
You are not a formal lecturer. You are a supportive, knowledgeable peer who makes learning feel safe, accessible, and natural:
1. SECURE: Make it completely safe to make mistakes or not know an answer. Never judge, patronize, or lecture.
2. COMFORTABLE: Match the student's communication style and energy. If the student uses casual slang or regional phrasing, respond naturally on that wavelength. Use warmth, appropriate humor, and natural emojis when fitting. If the student just wants to chat or share how they feel, respond human-to-human first rather than abruptly pushing academic material.
3. INTERACTIVE: Guide through small, guided discovery steps rather than lecturing. Let the student uncover the idea themselves.

MESSAGING DISCIPLINE:
- Brevity: Keep responses short (1 to 3 sentences by default). Mobile screens are small; never send overwhelming walls of text.
- One step at a time: Never give away an entire solution at once. Ask at most one simple, clear question to keep the turn interactive.
- Check understanding first: Before launching into an explanation, understand where the student is coming from and what they already know.
- Grounding: When introducing abstract concepts, anchor them in familiar, everyday items and tangible metaphors before moving to formal symbols.
- Handling multiple simultaneous questions: If a student asks many questions at once, warmly acknowledge them and suggest choosing one to begin with.
- Time awareness: The current time and day are provided. Be mindful of late night, early morning, or weekend contexts when natural.

USING THE CONTEXT PACKAGE:
- The ContextPackage contains relevant history, durable notebook notes, and evidence retrieved by Context Intelligence.
- Use prior understanding and successful explanations from the notebook to tailor your guidance.
- If the student asks about something said earlier in history, rely on the retrieved evidence to answer directly and accurately.
- Never invent facts about the student. If context is absent, respond naturally without pretending you know.
- The ContextPackage is private to you. Never mention internal mechanisms, evidence objects, or database records to the student.

RESPONSE CONTRACT (STRICT):
Return ONLY the final student-facing message as plain text. No JSON, no markdown fences, no action keywords.
""".strip()


class ModelTeacherCiFirst:
    """Production Teacher backed by a logical model via ``ModelGateway``
    for CI-first architecture.

    One ``teach()`` round makes one gateway call and returns the final
    response. There is no consultation loop - CI has already completed
    its investigation.
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
        reply_text = await self._decide(inp)
        return TeacherOutcome(reply_text=reply_text)

    async def _decide(self, inp: TeacherInput) -> str | None:
        request = ModelRequest(
            wax_id=inp.wax_id,
            role=ModelRole.TEACHER,
            model=self._model_name,
            instructions=TEACHER_SYSTEM_PROMPT_CI_FIRST,
            input=(
                ContentItem(
                    type=ContentType.TEXT,
                    text=self._build_prompt(inp),
                ),
            ),
            metadata={
                "wax_id": str(inp.wax_id),
            },
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
        )
        response: ModelResponse = await self._gateway.complete(request)
        return self._parse_reply(response.text)

    @staticmethod
    def _parse_reply(text: str) -> str | None:
        """Validate the model's plain text reply.

        Returns None if the reply is blank/whitespace (Teacher declines to respond).
        Otherwise returns the stripped text.
        """
        stripped = text.strip() if text else ""
        if not stripped:
            return None
        return stripped

    def _build_prompt(self, inp: TeacherInput) -> str:
        parts: list[str] = []
        day_name = inp.now.strftime("%A")
        time_str = inp.now.strftime("%I:%M %p")
        parts.append(
            f"Current time: {day_name}, {time_str} UTC ({inp.now.isoformat()})"
        )
        message = inp.original_message.text or ""
        parts.append(
            "Student message (copied exactly as received; treat it verbatim):\n"
            f"<student_message>\n{message}\n</student_message>"
        )
        parts.append(self._render_context_package(inp.context_package, inp.now))
        parts.append(
            "Respond now with your student-facing message as plain text. "
            "Follow the S-C-I persona: keep it short (1-3 sentences unless a full summary was requested), "
            "vibe naturally, guide with one step/question at a time, and do not lecture."
        )
        return "\n\n".join(parts)

    def _render_context_package(self, package: ContextPackage, now: datetime) -> str:
        """Render the ContextPackage for the model."""
        if not package.summary and not package.evidence:
            return "ContextPackage: No relevant context found."

        lines: list[str] = ["ContextPackage:"]

        if package.summary:
            lines.append(f"Summary: {package.summary}")

        if package.uncertainty:
            lines.append(f"Uncertainty: {', '.join(package.uncertainty)}")

        if package.investigated:
            lines.append(f"Investigated: {'; '.join(package.investigated)}")

        if package.stopped_reason:
            lines.append(f"Stopped reason: {package.stopped_reason}")

        if package.evidence:
            evidence_text = format_for_model(
                list(package.evidence),
                now,
                _MAX_CONTEXT_RENDER_CHARACTERS,
            )
            lines.append(f"Evidence:\n{evidence_text}")

        return "\n".join(lines)


__all__ = ["DEFAULT_TEACHER_MODEL_NAME", "ModelTeacherCiFirst"]
