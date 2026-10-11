"""Application shell around the Context Intelligence model."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from waxprep.context.evidence import format_for_model
from waxprep.context.model import ContextModel, ContextModelRequest
from waxprep.context.models import (
    ContextPackage,
    Evidence,
    InvestigationRequest,
)
from waxprep.context.system_prompt import CONTEXT_INTELLIGENCE_SYSTEM_PROMPT
from waxprep.context.tool_definitions import context_tool_definitions
from waxprep.context.tools import ContextTool, ToolInvocation

logger = logging.getLogger(__name__)


@dataclass
class ContextInvestigator:
    """The model decides what to investigate; the application enforces limits.

    A tool that fails is reported to the model and never crashes the turn.
    """

    model: ContextModel
    tools: dict[str, ContextTool] = field(
        default_factory=lambda: dict[str, ContextTool]()
    )

    async def investigate(self, request: InvestigationRequest) -> ContextPackage:
        query = request.query.strip()
        if not query:
            return ContextPackage(
                wax_id=request.wax_id,
                query=query,
                evidence=(),
                summary="No context required.",
                stopped_reason="empty_query",
                as_of=request.now,
            )

        evidence: list[Evidence] = []
        investigated: list[str] = []
        tool_calls = 0
        seen_ids: set[UUID] = set()
        limits = request.limits

        def model_request(visible: str) -> ContextModelRequest:
            return ContextModelRequest(
                system_prompt=CONTEXT_INTELLIGENCE_SYSTEM_PROMPT,
                user_query=query,
                evidence=visible,
                tools=context_tool_definitions(),
                max_output_tokens=limits.max_model_output_tokens,
                wax_id=request.wax_id,
                now=request.now,
            )

        for _step in range(limits.max_steps):
            if tool_calls >= limits.max_tool_calls:
                break
            visible = format_for_model(
                evidence, request.now, limits.max_context_characters
            )
            decision = await self.model.decide(model_request(visible))
            investigated.append(f"{decision.action}: {decision.reason}")
            if decision.action == "stop":
                break
            tool = self.tools.get(decision.action)
            if tool is None:
                investigated.append(f"missing_tool:{decision.action}")
                continue
            tool_calls += 1
            try:
                result = await tool.execute(
                    ToolInvocation(
                        wax_id=request.wax_id,
                        arguments=decision.arguments,
                        now=request.now,
                        trigger_entry_id=request.trigger_entry_id,
                    )
                )
            except Exception as exc:
                logger.exception("context tool %s failed", decision.action)
                investigated.append(
                    f"tool_failed:{decision.action}:{type(exc).__name__}"
                )
                continue
            for item in result.results:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                evidence.append(item)
                if len(evidence) >= limits.max_evidence:
                    break
            if len(evidence) >= limits.max_evidence:
                break

        visible = format_for_model(evidence, request.now, limits.max_context_characters)
        final = await self.model.finalize(model_request(visible))

        selected = evidence
        if final.selected_evidence_ids:
            wanted = set(final.selected_evidence_ids)
            selected = [e for e in evidence if str(e.id) in wanted] or evidence

        return ContextPackage(
            wax_id=request.wax_id,
            query=query,
            evidence=tuple(selected[: limits.max_evidence]),
            summary=final.summary,
            uncertainty=final.uncertainty,
            investigated=tuple(investigated),
            stopped_reason=final.stopped_reason,
            as_of=request.now,
        )
