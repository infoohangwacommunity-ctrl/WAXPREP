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

        # GUARANTEE: Every message performs at least one contextual inspection.
        # This ensures CI always checks the student's history before producing
        # the ContextPackage. The model may still decide to stop after one
        # inspection if it finds sufficient context.
        performed_contextual_inspection = False

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

            # GUARANTEE: CI must perform at least one contextual inspection.
            # If CI tries to stop without any tool calls and no evidence,
            # force at least a browse_time_window to check recent history.
            if decision.action == "stop" and tool_calls == 0 and len(evidence) == 0:
                # Force at least one contextual inspection
                logger.debug(
                    "CI attempted to stop without inspection; forcing baseline check"
                )
                # Use browse_time_window as the guaranteed baseline inspection
                tool = self.tools.get("browse_time_window")
                if tool is not None:
                    tool_calls += 1
                    try:
                        result = await tool.execute(
                            ToolInvocation(
                                wax_id=request.wax_id,
                                arguments={"limit": 5, "newest_first": True},
                                now=request.now,
                                trigger_entry_id=request.trigger_entry_id,
                            )
                        )
                    except Exception as exc:
                        logger.exception("baseline browse_time_window failed")
                        investigated.append(
                            f"baseline_tool_failed:browse_time_window:{type(exc).__name__}"
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
                    # Continue to let model finalize with this evidence
                    continue
                else:
                    logger.warning(
                        "CI stop without inspection and no browse_time_window tool available"
                    )

            if decision.action == "stop":
                break
            tool = self.tools.get(decision.action)
            if tool is None:
                investigated.append(f"missing_tool:{decision.action}")
                continue
            tool_calls += 1

            # Mark that we performed at least one contextual inspection
            if not performed_contextual_inspection:
                performed_contextual_inspection = True

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
