# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnnecessaryCast=false
"""Context Intelligence model adapter via Model Gateway."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from waxprep.context.exchange import format_exchange
from waxprep.context.model import (
    ContextModelDecision,
    ContextModelFinal,
    ContextModelRequest,
    ToolSelection,
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
    is enabled when the provider supports TOOL_CALLING. Independent lookups
    selected together (native multi tool-calls, or the ``actions`` JSON
    list) are returned as one batched decision so the runtime can execute
    them concurrently.
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
            calls = list(response.tool_calls)
            primary = calls[0]
            batch = tuple(
                ToolSelection(
                    action=call.name,
                    arguments=dict(call.arguments),
                    reason="native_tool_call",
                )
                for call in calls[1:]
            )
            return ContextModelDecision(
                action=primary.name,
                arguments=dict(primary.arguments),
                reason="native_tool_call",
                batch=batch,
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
            "You may call tools via the API tool interface, or return JSON "
            "with keys action, arguments, reason."
            if self._use_native_tools
            else "Return a single JSON object with keys action, arguments, reason."
        )
        exchange = self._render_exchange(request)
        return (
            f"{mode}\n"
            "action must be one of the tool names or 'stop'.\n"
            "When several lookups are independent of each other, select them "
            "together: set action to the first one and add the rest under the "
            "key 'actions' as a list of {action, arguments, reason} objects. "
            "Never select together a call whose arguments depend on another "
            "call's result.\n"
            "Interpret the student's intent from the message and evidence — "
            "do not rely on keyword routing.\n"
            f"Current time (UTC): {request.now.isoformat()}\n"
            f"Student message: {request.user_query}\n"
            f"{exchange}"
            f"Evidence so far:\n{request.evidence}\n"
            f"Tools: {tools}\n"
        )

    @staticmethod
    def _final_prompt(request: ContextModelRequest) -> str:
        exchange = GatewayContextModel._render_exchange(request)
        return (
            "Return a single JSON object with keys summary, "
            "selected_evidence_ids, uncertainty, stopped_reason, "
            "include_exchange, include_exchange_reason.\n"
            "Select only evidence that helps the Teacher for *this* intent; "
            "omit irrelevant history.\n"
            "Set include_exchange to true so the Teacher receives the recent "
            "exchange. The Teacher cannot see the thread any other way. "
            "Set it to false only when the exchange is genuinely irrelevant "
            "and nothing in it needs resolving, and say why in "
            "include_exchange_reason.\n"
            f"Current time (UTC): {request.now.isoformat()}\n"
            f"Student message: {request.user_query}\n"
            f"{exchange}"
            f"Evidence:\n{request.evidence}\n"
        )

    @staticmethod
    def _render_exchange(request: ContextModelRequest) -> str:
        """Render the supplied exchange material for the CI model.

        Framed as raw material the application handed over, so CI does
        not mistake it for something it already investigated and chosen.
        When there is nothing to hand over, that is said outright — a
        missing block would otherwise read as an oversight.
        """
        if request.exchange_empty or not request.exchange:
            return (
                "Recent exchange: none yet. This student has no prior "
                "exchange, so there is no thread to carry.\n\n"
            )
        body = format_exchange(request.exchange)
        if not body:
            return ""
        return (
            "Recent exchange (raw material supplied by the application, "
            "in order):\n"
            f"{body}\n\n"
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
        return ContextModelDecision(
            action=action,
            arguments=args,
            reason=reason,
            batch=_parse_batch(data.get("actions")),
        )

    def _parse_final(self, text: str) -> ContextModelFinal:
        data = _extract_json(text)
        if data is None:
            return ContextModelFinal(
                summary="Unable to form a structured context package.",
                selected_evidence_ids=(),
                uncertainty=("malformed_final_output",),
                stopped_reason="malformed_final",
                # A malformed answer must not silently strip the exchange:
                # the Teacher cannot see the thread any other way.
                include_exchange=True,
                include_exchange_reason="malformed_final_output",
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
        include_raw = data.get("include_exchange")
        # Default to include. Only an explicit false drops the exchange.
        include_exchange = include_raw is not False
        reason = str(data.get("include_exchange_reason") or "")
        if not include_exchange and not reason:
            reason = "ci_dropped_exchange_without_reason"
        return ContextModelFinal(
            summary=str(data.get("summary") or ""),
            selected_evidence_ids=tuple(ids),
            uncertainty=uncertainty,
            stopped_reason=str(data.get("stopped_reason") or "model_finalized"),
            include_exchange=include_exchange,
            include_exchange_reason=reason,
        )


def _parse_batch(raw: object) -> tuple[ToolSelection, ...]:
    """Parse the optional 'actions' list of independent tool calls.

    Anything malformed is skipped — a broken extra selection must never
    invalidate the primary call the model already made.
    """
    if not isinstance(raw, list):
        return ()
    out: list[ToolSelection] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        if not action:
            continue
        args = item.get("arguments")
        out.append(
            ToolSelection(
                action=action,
                arguments=dict(args) if isinstance(args, dict) else {},
                reason=str(item.get("reason") or ""),
            )
        )
    return tuple(out)


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
