"""Application shell around the Context Intelligence model."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from waxprep.context.model import ContextModel, ContextModelRequest
from waxprep.context.models import (
    ContextPackage,
    Evidence,
    InvestigationRequest,
)
from waxprep.context.system_prompt import CONTEXT_INTELLIGENCE_SYSTEM_PROMPT
from waxprep.context.tool_definitions import context_tool_definitions
from waxprep.context.tools import ContextTool


@dataclass
class ContextInvestigator:
    """Model decides what to investigate; application enforces limits."""

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
            )

        evidence: list[Evidence] = []
        investigated: list[str] = []
        tool_calls = 0
        seen_ids: set[UUID] = set()

        for _step in range(request.limits.max_steps):
            if tool_calls >= request.limits.max_tool_calls:
                break

            visible = self._format_evidence(
                evidence, request.limits.max_context_characters
            )
            decision = await self.model.decide(
                ContextModelRequest(
                    system_prompt=CONTEXT_INTELLIGENCE_SYSTEM_PROMPT,
                    user_query=query,
                    evidence=visible,
                    tools=context_tool_definitions(),
                    max_output_tokens=request.limits.max_model_output_tokens,
                )
            )
            investigated.append(f"{decision.action}: {decision.reason}")

            if decision.action == "stop":
                break

            tool = self.tools.get(decision.action)
            if tool is None:
                investigated.append(f"missing_tool:{decision.action}")
                break

            tool_calls += 1
            result = await tool.execute(request.wax_id, decision.arguments)
            for item in result.results:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                evidence.append(item)
                if len(evidence) >= request.limits.max_evidence:
                    break
            if len(evidence) >= request.limits.max_evidence:
                break

        visible = self._format_evidence(evidence, request.limits.max_context_characters)
        final = await self.model.finalize(
            ContextModelRequest(
                system_prompt=CONTEXT_INTELLIGENCE_SYSTEM_PROMPT,
                user_query=query,
                evidence=visible,
                tools=context_tool_definitions(),
                max_output_tokens=request.limits.max_model_output_tokens,
            )
        )

        selected = evidence
        if final.selected_evidence_ids:
            wanted = set(final.selected_evidence_ids)
            selected = [e for e in evidence if str(e.id) in wanted] or evidence

        return ContextPackage(
            wax_id=request.wax_id,
            query=query,
            evidence=tuple(selected[: request.limits.max_evidence]),
            summary=final.summary,
            uncertainty=final.uncertainty,
            investigated=tuple(investigated),
            stopped_reason=final.stopped_reason,
        )

    @staticmethod
    def _format_evidence(evidence: list[Evidence], max_chars: int) -> str:
        if not evidence:
            return "(no evidence yet)"
        parts: list[str] = []
        total = 0
        for item in evidence:
            chunk = f"[{item.id}] ({item.evidence_type.value}) {item.content}"
            if total + len(chunk) > max_chars:
                break
            parts.append(chunk)
            total += len(chunk)
        return "\n".join(parts)
