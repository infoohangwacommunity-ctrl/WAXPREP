# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Context Intelligence model adapter via Model Gateway."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from waxprep.context.model import (
    ContextModelDecision,
    ContextModelFinal,
    ContextModelRequest,
)
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
    ToolDefinition,
)


class GatewayContextModel:
    """Production-capable CI model using the existing Model Gateway.

    Reasoning effort defaults to medium (override with WAXPREP_REASONING_EFFORT).
    Tool selection remains application-validated; optional native tool_choice
    is enabled when the provider supports TOOL_CALLING.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_name: str = "context",
        reasoning_effort: str | None = None,
        use_native_tools: bool | None = None,
    ) -> None:
        self._gateway = gateway
        self._model_name = model_name
        self._reasoning_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else (os.environ.get("WAXPREP_REASONING_EFFORT") or "medium").lower()
        )
        if use_native_tools is None:
            use_native_tools = os.environ.get(
                "WAXPREP_CI_NATIVE_TOOLS", ""
            ).lower() in {"1", "true", "yes"}
        self._use_native_tools = use_native_tools

    async def decide(self, request: ContextModelRequest) -> ContextModelDecision:
        prompt = self._decision_prompt(request)
        tools = self._tool_defs(request) if self._use_native_tools else ()
        response = await self._gateway.complete(
            ModelRequest(
                wax_id=request.wax_id,
                role=ModelRole.CONTEXT,
                model=self._model_name,
                instructions=request.system_prompt,
                input=(ContentItem(type=ContentType.TEXT, text=prompt),),
                tools=tools,
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=self._reasoning_effort,
            )
        )
        if response.tool_calls:
            tc = response.tool_calls[0]
            return ContextModelDecision(
                action=tc.name,
                arguments=dict(tc.arguments),
                reason="native_tool_call",
            )
        return self._parse_decision(response.text)

    async def finalize(self, request: ContextModelRequest) -> ContextModelFinal:
        prompt = self._final_prompt(request)
        response = await self._gateway.complete(
            ModelRequest(
                wax_id=request.wax_id,
                role=ModelRole.CONTEXT,
                model=self._model_name,
                instructions=request.system_prompt,
                input=(ContentItem(type=ContentType.TEXT, text=prompt),),
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=self._reasoning_effort,
            )
        )
        return self._parse_final(response.text)

    def _tool_defs(self, request: ContextModelRequest) -> tuple[ToolDefinition, ...]:
        out: list[ToolDefinition] = []
        for raw in request.tools or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "")
            if not name:
                continue
            out.append(
                ToolDefinition(
                    name=name,
                    description=str(raw.get("description") or ""),
                    input_schema=dict(raw.get("parameters") or {}),
                )
            )
        return tuple(out)

    def _decision_prompt(self, request: ContextModelRequest) -> str:
        tools = json.dumps(request.tools, ensure_ascii=True)
        mode = (
            "You may call a tool via the API tool interface, or return JSON "
            "with keys action, arguments, reason."
            if self._use_native_tools
            else "Return a single JSON object with keys action, arguments, reason."
        )
        return (
            f"{mode}\n"
            "action must be one of the tool names or 'stop'.\n"
            "Interpret the student's intent from the message and evidence — "
            "do not rely on keyword routing.\n"
            f"Current time (UTC): {request.now.isoformat()}\n"
            f"Student message: {request.user_query}\n"
            f"Evidence so far:\n{request.evidence}\n"
            f"Tools: {tools}\n"
        )

    def _final_prompt(self, request: ContextModelRequest) -> str:
        return (
            "Return a single JSON object with keys summary, selected_evidence_ids, "
            "uncertainty, stopped_reason.\n"
            "Select only evidence that helps the Teacher for *this* intent; "
            "omit irrelevant history.\n"
            f"Current time (UTC): {request.now.isoformat()}\n"
            f"Student message: {request.user_query}\n"
            f"Evidence:\n{request.evidence}\n"
        )

    def _parse_decision(self, text: str) -> ContextModelDecision:
        data = _extract_json(text)
        if data is None:
            return ContextModelDecision(
                action="stop",
                arguments={},
                reason="malformed model decision; stopping safely",
            )
        action = str(data.get("action") or "stop")
        args = data.get("arguments")
        if not isinstance(args, dict):
            args = {}
        reason = str(data.get("reason") or "")
        return ContextModelDecision(action=action, arguments=args, reason=reason)

    def _parse_final(self, text: str) -> ContextModelFinal:
        data = _extract_json(text)
        if data is None:
            return ContextModelFinal(
                summary="Unable to form a structured context package.",
                selected_evidence_ids=(),
                uncertainty=("malformed_final_output",),
                stopped_reason="malformed_final",
            )
        ids_raw = data.get("selected_evidence_ids") or ()
        ids: list[str] = []
        if isinstance(ids_raw, list):
            ids = [str(x) for x in ids_raw]
        unc = data.get("uncertainty") or ()
        uncertainty: tuple[str, ...] = ()
        if isinstance(unc, list):
            uncertainty = tuple(str(x) for x in unc)
        elif isinstance(unc, str) and unc:
            uncertainty = (unc,)
        return ContextModelFinal(
            summary=str(data.get("summary") or ""),
            selected_evidence_ids=tuple(ids),
            uncertainty=uncertainty,
            stopped_reason=str(data.get("stopped_reason") or "model_finalized"),
        )


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
