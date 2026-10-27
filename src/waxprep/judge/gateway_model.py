"""Gateway-backed judge model — same execution boundary as CI and Teacher.

Uses the existing ``ModelGateway`` with the logical ``judge`` model
(``ModelRole.JUDGE``) so the judge can be pointed at a different
endpoint than the teacher. Self-preference bias is a known LLM-judge
failure: a judge that shares the teacher's model systematically favors
the teacher's style. The separate role makes mitigation a deployment
choice, not a code change.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from waxprep.judge.models import JUDGE_SYSTEM_PROMPT, JudgeOutputError, JudgeRequest
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
)

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def extract_json_object(text: str) -> dict[str, Any]:
    """Direct JSON first, then the first ``{...}`` block (mirrors the CI adapter)."""
    parsed: object = None
    try:
        parsed = json.loads(text)
    except ValueError:
        match = _JSON_BLOCK.search(text)
        if match is None:
            raise JudgeOutputError("judge output contained no JSON object") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise JudgeOutputError("judge output must be a JSON object")
    return parsed


class GatewayJudgeModel:
    """Judge adapter over the provider-neutral ``ModelGateway``."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_name: str = "judge",
        reasoning_effort: str | None = None,
        max_output_tokens: int = 1600,
    ) -> None:
        self._gateway = gateway
        self._model_name = model_name
        self._reasoning_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else (os.environ.get("WAXPREP_REASONING_EFFORT") or "medium").lower()
        )
        self._max_output_tokens = max_output_tokens

    async def evaluate(self, request: JudgeRequest) -> dict[str, Any]:
        prompt = self._prompt(request)
        response = await self._gateway.complete(
            ModelRequest(
                wax_id=request.wax_id,
                role=ModelRole.JUDGE,
                model=self._model_name,
                instructions=JUDGE_SYSTEM_PROMPT,
                input=(ContentItem(type=ContentType.TEXT, text=prompt),),
                metadata={"wax_id": str(request.wax_id)},
                max_output_tokens=self._max_output_tokens,
                reasoning_effort=self._reasoning_effort,
            )
        )
        return extract_json_object(response.text or "")

    @staticmethod
    def _prompt(request: JudgeRequest) -> str:
        parts: list[str] = [
            "Evaluate the tutoring quality in the window below.",
            f"Window: {request.window_turns} student-tutor pair(s).",
        ]
        previous = request.previous_payload
        if previous is not None:
            compact = {
                "previous_rating": previous.get("rating"),
                "previous_trend": previous.get("trend"),
                "previous_focus_areas": previous.get("focus_areas"),
                "previous_tutor_guidance": previous.get("tutor_guidance"),
            }
            parts.append(
                "Previous assessment (trend context only — judge this "
                "window on its own evidence):\n"
                + json.dumps(compact, ensure_ascii=True)
            )
        parts.append("Window (oldest first):\n" + request.window_text)
        parts.append("Return only the JSON object described in your instructions.")
        return "\n\n".join(parts)


__all__ = ["GatewayJudgeModel", "extract_json_object"]
