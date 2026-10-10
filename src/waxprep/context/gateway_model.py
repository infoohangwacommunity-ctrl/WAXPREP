# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Context Intelligence model adapter via Model Gateway."""

from __future__ import annotations

import json
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
)


class GatewayContextModel:
    """Production-capable CI model using the existing Model Gateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_name: str = "context",
        wax_id_for_requests: object | None = None,
    ) -> None:
        self._gateway = gateway
        self._model_name = model_name
        # Trusted WAX ID for billing/isolation metadata on gateway requests
        from waxprep.domain.identifiers import new_wax_id

        self._wax_id = wax_id_for_requests or new_wax_id()

    async def decide(self, request: ContextModelRequest) -> ContextModelDecision:
        prompt = self._decision_prompt(request)
        response = await self._gateway.complete(
            ModelRequest(
                wax_id=self._wax_id,  # type: ignore[arg-type]
                role=ModelRole.CONTEXT,
                model=self._model_name,
                instructions=request.system_prompt,
                input=(ContentItem(type=ContentType.TEXT, text=prompt),),
                max_output_tokens=request.max_output_tokens,
            )
        )
        return self._parse_decision(response.text)

    async def finalize(self, request: ContextModelRequest) -> ContextModelFinal:
        prompt = self._final_prompt(request)
        response = await self._gateway.complete(
            ModelRequest(
                wax_id=self._wax_id,  # type: ignore[arg-type]
                role=ModelRole.CONTEXT,
                model=self._model_name,
                instructions=request.system_prompt,
                input=(ContentItem(type=ContentType.TEXT, text=prompt),),
                max_output_tokens=request.max_output_tokens,
            )
        )
        return self._parse_final(response.text)

    def _decision_prompt(self, request: ContextModelRequest) -> str:
        tools = json.dumps(request.tools, ensure_ascii=True)
        return (
            "Return a single JSON object with keys action, arguments, reason.\n"
            "action must be one of the tool names or 'stop'.\n"
            f"Student message: {request.user_query}\n"
            f"Evidence so far:\n{request.evidence}\n"
            f"Tools: {tools}\n"
        )

    def _final_prompt(self, request: ContextModelRequest) -> str:
        return (
            "Return a single JSON object with keys summary, selected_evidence_ids, "
            "uncertainty, stopped_reason.\n"
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
                stopped_reason="malformed_model_output",
            )
        ids_raw = data.get("selected_evidence_ids") or ()
        if isinstance(ids_raw, list):
            ids = tuple(str(x) for x in ids_raw)
        else:
            ids = ()
        unc_raw = data.get("uncertainty") or ()
        if isinstance(unc_raw, list):
            unc = tuple(str(x) for x in unc_raw)
        else:
            unc = ()
        return ContextModelFinal(
            summary=str(data.get("summary") or ""),
            selected_evidence_ids=ids,
            uncertainty=unc,
            stopped_reason=str(data.get("stopped_reason") or "model_complete"),
        )


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
