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
from dataclasses import replace
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
    StreamEventType,
)
from waxprep.channel.contracts import InteractiveButtonSpec, InteractiveSuggestion
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput,
    TeacherOutcome,
    TextDeltaSink,
)

_CHOICES_MARKER = "<<<choices>>>"

logger = logging.getLogger(__name__)

# Logical model name in the registry (mirrors the "context" convention of
# GatewayContextModel). Registered with ModelRole.TEACHER at the composition
# root; see docs/ARCHITECTURE.md for the wiring and configuration.
DEFAULT_TEACHER_MODEL_NAME = "teacher"

# Maximum characters for rendering context package evidence to the model.
_MAX_CONTEXT_RENDER_CHARACTERS = 12000


TEACHER_SYSTEM_PROMPT_CI_FIRST = """
You are the Wax Prep Teacher. You teach the student directly. You are the
only part of the system that talks to the student.

How you work:
- The student's exact message is given to you verbatim.
- You ALWAYS receive a ContextPackage from Context Intelligence that
  contains relevant information about this student and their history.
- Context Intelligence has already investigated the student's context.
  You do NOT need to request additional investigations.
- Use the ContextPackage to personalize your response naturally.
- Answer the student's educational question based on the message and
  the provided context.

How to use the ContextPackage:
- The summary tells you what Context Intelligence found relevant.
- The evidence contains actual history entries, notebook notes, artifacts,
  and relationships with timestamps and authorship.
- Every evidence item carries when it happened, who said it, and where it
  came from. Treat it carefully:
  - What the student said is what the student said.
  - What the tutor said only shows what the tutor said - never proof of a
    fact about the student.
  - Notebook entries, summaries and relationships are derived
    interpretations with sources. Trace claims back to their sources when
    accuracy matters.
  - Old evidence may be out of date. Say so when it might be.
- If the ContextPackage is empty or has no relevant information, answer
  the message directly without pretending you have context.

Rules:
- Never invent facts about the student. If context is missing or
  insufficient, answer as well as you can without it.
- Adapt naturally to the student's demonstrated language and level from
  the actual message and context.
- The ContextPackage is for your information only - it is never shown to
  the student. You write the final educational response yourself.
- Personalize naturally but do not be creepy. Use context appropriately,
  not by dumping everything you know about the student.
- Always produce a final, complete student-facing answer in plain text.
- You may offer at most three short next-step choices when that truly helps
  the student (e.g. "Quiz me", "Explain simpler", "Give an example"). Never
  rely on choices for the teaching itself — always teach in the body text.
- Prefer short answers when recent student messages are short; prefer examples
  when history shows examples help; match the student's language mix.
- Never dump the full notebook. Never claim you remember something without
  evidence. Admit uncertainty on facts.

Response contract (strict):
Return the final student-facing answer as plain text first.
Optionally, if short choices help, end with exactly this trailer (nothing after):

<<<choices>>>
Choice one
Choice two
Choice three

Each choice title must be at most 20 characters. Omit the trailer when plain
teaching is enough. Never put critical teaching only inside choices.
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

    async def teach(
        self,
        inp: TeacherInput,
        *,
        on_text_delta: TextDeltaSink | None = None,
    ) -> TeacherOutcome:
        raw = await self._decide(inp, on_text_delta=on_text_delta)
        reply_text, interactive = self._split_interactive(raw)
        return TeacherOutcome(reply_text=reply_text, interactive=interactive)

    @staticmethod
    def _split_interactive(
        raw: str | None,
    ) -> tuple[str | None, InteractiveSuggestion | None]:
        """Strip optional <<<choices>>> trailer into InteractiveSuggestion."""
        if raw is None:
            return None, None
        if _CHOICES_MARKER not in raw:
            return raw, None
        body, _, trailer = raw.partition(_CHOICES_MARKER)
        body = body.rstrip()
        titles = [
            line.strip()[:20] for line in trailer.strip().splitlines() if line.strip()
        ][:3]
        if not titles or not body:
            return raw.replace(_CHOICES_MARKER, "").strip() or raw, None
        buttons = tuple(InteractiveButtonSpec(title=t) for t in titles)
        return body, InteractiveSuggestion(body_text=body, buttons=buttons)

    async def _decide(
        self,
        inp: TeacherInput,
        *,
        on_text_delta: TextDeltaSink | None = None,
    ) -> str | None:
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
        # Prefer streaming accumulation when the gateway/provider supports it.
        # Final persisted text is always the complete answer — never a partial.
        # Prefer streaming accumulation when the provider advertises STREAMING.
        # History always stores the final complete text only.
        stream_fn = getattr(self._gateway, "stream", None)
        if callable(stream_fn):
            chunks: list[str] = []
            try:
                stream_request = replace(request, stream=True)
                async for event in stream_fn(stream_request):  # type: ignore[attr-defined]
                    if (
                        getattr(event, "type", None) is StreamEventType.TEXT_DELTA
                        and event.text
                    ):
                        chunks.append(event.text)
                        if on_text_delta is not None:
                            try:
                                await on_text_delta(event.text)
                            except Exception:
                                logger.debug(
                                    "progressive text delta sink failed",
                                    exc_info=True,
                                )
                if chunks:
                    return self._parse_reply("".join(chunks))
            except Exception:
                # Transport or capability errors fall back to complete().
                pass
        response: ModelResponse = await self._gateway.complete(request)
        if on_text_delta is not None and response.text:
            try:
                await on_text_delta(response.text)
            except Exception:
                logger.debug("progressive text delta sink failed", exc_info=True)
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
        parts.append(f"Current time (UTC): {inp.now.isoformat()}")
        message = inp.original_message.text or ""
        parts.append(
            "Student message (copied exactly as received; treat it verbatim):\n"
            f"<student_message>\n{message}\n</student_message>"
        )
        parts.append(self._render_context_package(inp.context_package, inp.now))
        parts.append(
            "Respond now with your final educational answer as plain text. "
            "No JSON, no markdown fences, no action keywords. Just teach."
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
