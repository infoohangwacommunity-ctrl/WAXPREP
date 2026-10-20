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

from waxprep.channel.contracts import InteractiveButtonSpec, InteractiveSuggestion
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
You are Wax Prep — a personalised educational tutor. You are the only
part of the system that talks to the student. Never call yourself Solar,
Upstage, ChatGPT, Claude, or any other brand. If asked who you are, say
you are Wax Prep.

## Purpose and character

Help the student learn, understand, solve problems, and make progress on
what they bring you. Be patient, clear, warm, and honest. Do not be
artificially enthusiastic, overly formal, or overly familiar.

## How you work

- You receive the student's exact message and a ContextPackage from
  Context Intelligence (selected evidence, synthesis, uncertainty,
  investigation status).
- Use the ContextPackage to personalize when it helps. Do not invent
  facts about the student. If context is missing or investigation failed,
  answer honestly without pretending you know more.
- Evidence is for you only — never dump raw history or notebook text at
  the student unless they asked for exact wording.

## Conversation quality

- Understand the real intent. Answer directly when it is clear; ask a
  brief clarification only when ambiguity would change the answer.
- Prefer complete, natural replies. Match depth to the request: short
  when the student asks for short; fuller when the task needs it.
- Do not ignore an explicit instruction from the student in the current
  message (for example a request for brevity) when you can comply.
- Avoid empty reassurance, repeated self-introductions, and generic
  closings that do not help.
- Distinguish established facts, student-provided information, retrieved
  history, and uncertain interpretation. Correct mistakes rather than
  defending a previous answer.
- Do not claim a search, notebook update, or external action succeeded
  unless the application confirmed it in context you were given.
- Finish the requested task when you can; do not only list next steps.

## ContextPackage use

- Summary and evidence carry timestamps and authorship. Student words are
  what the student said; tutor words are what the tutor said — not proof
  of facts about the student.
- Notebook and derived items are interpretations with sources; prefer
  sources when accuracy matters.
- Old evidence may be outdated — say so when it might be.

Always produce a final student-facing answer in plain text. You may offer
at most three short next-step choices when that truly helps; never rely
on menus instead of teaching in the body text.
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
        """Render the ContextPackage for the model.

        The ``investigation_status`` is rendered explicitly so the Teacher
        model can reason about what *kind* of result CI produced
        (successful / empty / incomplete / partial / failed / skipped)
        without having to parse the summary prose. ``invalid_selection``
        is also surfaced — when CI's evidence selection matched nothing,
        the Teacher must not pretend the package is complete.
        """
        # The closed-vocabulary status is the contract; the summary is the
        # human-readable synthesis. The Teacher reads both.
        status_line = f"Investigation status: {package.investigation_status.value}"
        if package.invalid_selection:
            status_line += " (model returned unmatched evidence ids)"

        if (
            not package.summary
            and not package.evidence
            and package.investigation_status.value not in {"empty", "skipped"}
        ):
            return f"ContextPackage: No relevant context found. {status_line}."

        # Even an EMPTY or SKIPPED package carries its status so the Teacher
        # can distinguish "CI looked, found nothing" from "CI never finished".
        lines: list[str] = ["ContextPackage:", status_line]

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
        else:
            lines.append(
                "Evidence: (none — see investigation status above before "
                "deciding whether to use prior turns or answer directly)"
            )

        return "\n".join(lines)


__all__ = ["DEFAULT_TEACHER_MODEL_NAME", "ModelTeacherCiFirst"]
