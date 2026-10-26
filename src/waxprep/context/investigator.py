"""Application shell around the Context Intelligence model.

The model decides what to investigate; the application executes the chosen
tools, enforces mechanical budgets, and refuses to silently fabricate a
result when something goes wrong. The package this produces always carries
an explicit ``investigation_status`` so the Teacher can distinguish a real
"no relevant context" from a "CI never finished" — they are not the same.

Three concrete defects called out in the architecture blueprint are fixed
here and made regression-testable:

1. The previous investigator set a "did we inspect" flag and never read
   it. That dead state is gone. The completion invariant is now expressed
   through the closed-vocabulary ``InvestigationStatus`` enum: an empty
   package is only allowed to be labelled ``EMPTY`` (i.e. CI genuinely
   looked and found nothing) when at least one tool was actually executed.
   Otherwise the runtime records the stop as ``INCOMPLETE`` or ``FAILED``
   — the model never produced a real investigation.

2. Invalid evidence selection used to silently fall back to *all* gathered
   evidence when the model returned ids that matched nothing. This made a
   broken selection indistinguishable from "use everything". The fallback
   is removed. When the model returns ids that match no gathered evidence,
   the runtime sets ``invalid_selection=True``, records the failure in the
   ``investigated`` audit trail, and ships an empty evidence tuple. The
   Teacher can see the failure and treat the package honestly.

3. A "stop without any tool calls and no evidence" used to force one
   ``browse_time_window`` call. That fallback stays (it is a real attempt
   to look), but it now contributes to the completion status honestly: if
   it returns nothing, the status is ``EMPTY``; if it fails, the status
   is ``PARTIAL``.
"""

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
    InvestigationStatus,
)
from waxprep.context.system_prompt import CONTEXT_INTELLIGENCE_SYSTEM_PROMPT
from waxprep.context.tool_definitions import context_tool_definitions
from waxprep.context.tools import ContextTool, ToolInvocation

logger = logging.getLogger(__name__)


@dataclass
class ContextInvestigator:
    """The model decides what to investigate; the application enforces limits.

    A tool that fails is reported to the model and never crashes the turn.
    The runtime computes the final ``investigation_status`` itself — it is
    not the model's claim.
    """

    model: ContextModel
    tools: dict[str, ContextTool] = field(
        default_factory=lambda: dict[str, ContextTool]()
    )

    async def investigate(self, request: InvestigationRequest) -> ContextPackage:
        raw_query = request.query.strip()
        query = raw_query
        # Tell CI the durable id of the message already saved in history so
        # it can cite that entry when proposing notebook updates — without
        # inventing ids or hardcoding topics.
        if raw_query and request.trigger_entry_id is not None:
            query = (
                f"{raw_query}\n\n"
                f"[trigger_history_entry_id={request.trigger_entry_id}] "
                "This is the already-saved history id for the current student "
                "message. When proposing a notebook change about what the "
                "student just said, cite object_type=history and this id in "
                "evidence_refs after you have inspected relevant context."
            )
        if not raw_query:
            # A blank message is a deliberate SKIPPED, not a successful
            # investigation of nothing. The Teacher can branch on this.
            return ContextPackage(
                wax_id=request.wax_id,
                query=query,
                evidence=(),
                summary="No context required for a blank message.",
                exchange=request.exchange,
                stopped_reason="empty_query",
                as_of=request.now,
                investigation_status=InvestigationStatus.SKIPPED,
            )

        evidence: list[Evidence] = []
        investigated: list[str] = []
        tool_calls = 0
        failed_tool_calls = 0
        seen_ids: set[UUID] = set()
        limits = request.limits
        stopped_by_model = False
        hit_step_budget = False
        hit_tool_budget = False
        # Hitting the evidence cap is a *legitimate* stop, not a failure.
        # The runtime tells the model "you have enough, finalise now" —
        # distinct from hitting the step or tool budget, which is genuinely
        # running out of room. The status computation uses this to
        # distinguish SUCCESSFUL (evidence cap reached with evidence in
        # hand) from INCOMPLETE (step/tool budget reached before the
        # model finished).
        hit_evidence_budget = False

        def model_request(visible: str) -> ContextModelRequest:
            return ContextModelRequest(
                system_prompt=CONTEXT_INTELLIGENCE_SYSTEM_PROMPT,
                user_query=query,
                evidence=visible,
                tools=context_tool_definitions(),
                max_output_tokens=limits.max_model_output_tokens,
                wax_id=request.wax_id,
                now=request.now,
                exchange=request.exchange,
                exchange_empty=not request.exchange,
            )

        for _step in range(limits.max_steps):
            if tool_calls >= limits.max_tool_calls:
                hit_tool_budget = True
                break
            visible = format_for_model(
                evidence, request.now, limits.max_context_characters
            )
            decision = await self.model.decide(model_request(visible))
            investigated.append(f"{decision.action}: {decision.reason}")

            # GUARANTEE: CI must perform at least one contextual inspection.
            # If CI tries to stop without any tool calls and no evidence,
            # force at least one browse_time_window to check recent history.
            # This is the *only* place the application overrides the model,
            # and it is documented in the system prompt as a baseline check.
            if decision.action == "stop" and tool_calls == 0 and len(evidence) == 0:
                logger.debug(
                    "CI attempted to stop without inspection; "
                    "forcing baseline browse_time_window"
                )
                tool = self.tools.get("browse_time_window")
                if tool is None:
                    # No baseline tool available: record and accept the
                    # model's stop. Status will be EMPTY (we genuinely have
                    # no evidence and no way to look).
                    investigated.append("missing_tool:browse_time_window")
                    stopped_by_model = True
                    break
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
                    failed_tool_calls += 1
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
                        hit_evidence_budget = True
                        break
                # Continue to let the model decide what to do with this
                # baseline evidence (stop, search more, etc.).
                continue

            if decision.action == "stop":
                stopped_by_model = True
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
                failed_tool_calls += 1
                investigated.append(
                    f"tool_failed:{decision.action}:{type(exc).__name__}"
                )
                continue
            # Surface write-side proposal outcomes in the investigated
            # audit trail. Read tools return evidence; write tools return
            # empty results but metadata describing the proposal's
            # acceptance/rejection and what was created/invalidated.
            # The Teacher (and any operator) can see what CI proposed
            # and what the application actually did — the model's claim
            # of an operation is not sufficient.
            if result.metadata:
                accepted = result.metadata.get("accepted")
                operation = result.metadata.get("operation") or decision.action
                if accepted is True:
                    investigated.append(f"proposal_accepted:{operation}")
                elif accepted is False:
                    investigated.append(
                        f"proposal_rejected:{operation}:{result.metadata.get('reason', '')[:80]}"
                    )
                # Surface the structural counts so the audit trail is
                # self-explanatory without re-reading the metadata dict.
                inv_count = result.metadata.get("invalidated_count")
                if inv_count:
                    investigated.append(f"invalidated:{inv_count}")
                del_id = result.metadata.get("deletion_request_id")
                if del_id:
                    investigated.append(f"deletion_request:{del_id}")
            for item in result.results:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                evidence.append(item)
                if len(evidence) >= limits.max_evidence:
                    break
            if len(evidence) >= limits.max_evidence:
                # Hitting the evidence cap mid-investigation is a
                # legitimate stop: the runtime is telling the model
                # "you have enough evidence, finalise now". It is NOT
                # the same as hitting the step budget (genuinely out
                # of room). The status computation distinguishes them.
                hit_evidence_budget = True
                break
        else:
            # The for-loop fell through without `break`, meaning we hit
            # the step budget. This is distinct from the model saying
            # "stop" — it is a mechanical cap.
            hit_step_budget = True

        # Optional notebook stewardship step: if CI never proposed a lifecycle
        # change, give one more decision focused on whether a durable notebook
        # update is justified. The model may still stop; we do not force a write.
        if not any(x.startswith("proposal_accepted:") for x in investigated):
            if tool_calls < limits.max_tool_calls and not hit_step_budget:
                visible = format_for_model(
                    evidence, request.now, limits.max_context_characters
                )
                steward_query = (
                    query + "\n\n[stewardship] If this turn established durable "
                    "understanding useful for future personalization, and you "
                    "have evidence ids to cite, call propose_lifecycle_change. "
                    "Otherwise stop. Do not invent evidence ids."
                )
                steward_req = ContextModelRequest(
                    system_prompt=CONTEXT_INTELLIGENCE_SYSTEM_PROMPT,
                    user_query=steward_query,
                    evidence=visible,
                    tools=context_tool_definitions(),
                    max_output_tokens=limits.max_model_output_tokens,
                    wax_id=request.wax_id,
                    now=request.now,
                    exchange=request.exchange,
                    exchange_empty=not request.exchange,
                )
                decision = await self.model.decide(steward_req)
                investigated.append(f"stewardship:{decision.action}: {decision.reason}")
                if (
                    decision.action not in ("stop", "final")
                    and decision.action in self.tools
                ):
                    tool = self.tools.get(decision.action)
                    if tool is not None and tool_calls < limits.max_tool_calls:
                        tool_calls += 1
                        try:
                            result = await tool.execute(
                                ToolInvocation(
                                    wax_id=request.wax_id,
                                    arguments=decision.arguments or {},
                                    now=request.now,
                                    trigger_entry_id=request.trigger_entry_id,
                                )
                            )
                        except Exception as exc:
                            logger.exception("stewardship tool failed")
                            failed_tool_calls += 1
                            investigated.append(
                                f"stewardship_tool_failed:{decision.action}:{type(exc).__name__}"
                            )
                        else:
                            if result.metadata:
                                accepted = result.metadata.get("accepted")
                                operation = (
                                    result.metadata.get("operation") or decision.action
                                )
                                if accepted is True:
                                    investigated.append(
                                        f"proposal_accepted:{operation}"
                                    )
                                elif accepted is False:
                                    investigated.append(
                                        f"proposal_rejected:{operation}:"
                                        f"{str(result.metadata.get('reason', ''))[:80]}"
                                    )
                            for item in result.results:
                                if item.id in seen_ids:
                                    continue
                                seen_ids.add(item.id)
                                evidence.append(item)

        # Finalize with whatever evidence we have.
        visible = format_for_model(evidence, request.now, limits.max_context_characters)
        final = await self.model.finalize(model_request(visible))

        # Evidence selection with an explicit, observable contract.
        # Old behaviour: `[e for e in evidence if str(e.id) in wanted] or evidence`
        # — this silently included *everything* when the model returned ids
        # that matched nothing. The new contract:
        #   - empty selection => "use all gathered evidence" (model delegates)
        #   - non-empty selection with at least one match => use only matches
        #   - non-empty selection with NO match => record the failure, ship an
        #     empty evidence tuple. We do NOT silently inflate the package.
        invalid_selection = False
        final_uncertainty: tuple[str, ...] = final.uncertainty
        if not final.selected_evidence_ids:
            selected = list(evidence)
        else:
            wanted = {str(sid) for sid in final.selected_evidence_ids}
            selected = [e for e in evidence if str(e.id) in wanted]
            if not selected:
                invalid_selection = True
                investigated.append(
                    "invalid_evidence_selection:" + ",".join(sorted(wanted))
                )
                # Surface the failure in uncertainty so the Teacher can
                # reason about it without parsing the audit log.
                final_uncertainty = final.uncertainty + (
                    "context_intelligence_returned_unmatched_evidence_ids",
                )

        # The exchange decision belongs to CI alone. The application
        # supplied the material; CI decides whether the Teacher receives
        # it. The default is *include*: the Teacher cannot see the thread
        # any other way, so silence must never strip it. A drop is an
        # explicit decision and it is recorded, never invisible.
        if final.include_exchange:
            package_exchange = request.exchange
            if package_exchange:
                investigated.append(f"exchange:included:{len(package_exchange)}")
        else:
            package_exchange = ()
            drop_reason = (final.include_exchange_reason or "no_reason_given")[:80]
            investigated.append(f"exchange:dropped:{drop_reason}")
            final_uncertainty = final_uncertainty + (f"exchange_dropped:{drop_reason}",)

        status = _compute_status(
            stopped_by_model=stopped_by_model,
            hit_step_budget=hit_step_budget,
            hit_tool_budget=hit_tool_budget,
            hit_evidence_budget=hit_evidence_budget,
            tool_calls=tool_calls,
            failed_tool_calls=failed_tool_calls,
            evidence_count=len(selected),
            invalid_selection=invalid_selection,
        )

        # The investigated audit log carries enough to tell a future
        # reviewer (or a regression test) exactly what the runtime did.
        investigated.append(f"investigation_status:{status.value}")
        investigated.append(f"tool_calls:{tool_calls}")
        if failed_tool_calls:
            investigated.append(f"failed_tool_calls:{failed_tool_calls}")
        if hit_step_budget:
            investigated.append("stop_reason:step_budget_reached")
        elif hit_tool_budget:
            investigated.append("stop_reason:tool_budget_reached")
        elif hit_evidence_budget:
            investigated.append("stop_reason:evidence_budget_reached")
        elif stopped_by_model:
            investigated.append("stop_reason:model_finalized")

        logger.info(
            "ci_investigation_finished",
            extra={
                "wax_id": str(request.wax_id),
                "trigger_entry_id": str(request.trigger_entry_id)
                if request.trigger_entry_id
                else None,
                "status": status.value,
                "tool_calls": tool_calls,
                "failed_tool_calls": failed_tool_calls,
                "evidence_count": len(selected),
                "invalid_selection": invalid_selection,
                "stopped_by_model": stopped_by_model,
                "hit_step_budget": hit_step_budget,
                "hit_tool_budget": hit_tool_budget,
            },
        )

        return ContextPackage(
            wax_id=request.wax_id,
            query=query,
            evidence=tuple(selected[: limits.max_evidence]),
            summary=final.summary,
            exchange=package_exchange,
            uncertainty=final_uncertainty,
            investigated=tuple(investigated),
            stopped_reason=final.stopped_reason,
            as_of=request.now,
            investigation_status=status,
            invalid_selection=invalid_selection,
        )


def _compute_status(
    *,
    stopped_by_model: bool,
    hit_step_budget: bool,
    hit_tool_budget: bool,
    hit_evidence_budget: bool,
    tool_calls: int,
    failed_tool_calls: int,
    evidence_count: int,
    invalid_selection: bool,
) -> InvestigationStatus:
    """Decide the package's investigation status from runtime facts.

    The ordering matters: the most severe cause wins, because the Teacher
    needs to know the *worst* thing that happened, not the average.

    1. FAILED: the investigation never got off the ground. Either no tool
       was called at all and no model stop happened, or every tool call
       failed.
    2. INCOMPLETE: a hard mechanical *budget* (step or tool) was hit
       before the model signalled stop. The model never said "I'm done".
       Hitting the *evidence* cap is NOT incomplete — it is a legitimate
       "you have enough, finalise now" stop.
    3. PARTIAL: the model finalised, but at least one tool failed along
       the way. Distinct from INCOMPLETE because the cause is tool failure,
       not budget exhaustion.
    4. EMPTY: the model finalised cleanly, tools ran without failure,
       but no evidence was returned (or the model's selection matched
       nothing — which we treat as an honest empty, not a success).
    5. SUCCESSFUL: the model finalised and at least one piece of evidence
       survived selection. This includes the case where the evidence cap
       was reached — that is a successful "enough evidence" stop.
    """
    # Hitting the evidence cap is a successful stop, *provided* we have
    # at least one piece of evidence in hand (which the cap implies)
    # AND the model's final selection did not invalidate the result.
    # The Teacher should see SUCCESSFUL — the runtime told the model
    # "you have enough, finalise now", which is exactly what happened.
    # This must be checked before the FAILED check below, because
    # `stopped_by_model` is False when we broke out for the evidence cap.
    if hit_evidence_budget and evidence_count > 0 and not invalid_selection:
        # If some tools failed along the way, the package is partial —
        # but partial of a successful investigation, not of a failed one.
        # Still surface the tool failure honestly.
        if failed_tool_calls > 0 and failed_tool_calls < tool_calls:
            return InvestigationStatus.PARTIAL
        return InvestigationStatus.SUCCESSFUL

    # If the model returned an invalid evidence selection (ids that
    # matched nothing gathered), the runtime ships an empty evidence
    # tuple and sets invalid_selection=True. The Teacher must see this
    # as EMPTY (with the invalid_selection flag), not as a clean
    # SUCCESSFUL — regardless of why the investigation stopped. This
    # case can arise even when the evidence cap was hit, if the model
    # then returned an invalid final selection.
    if invalid_selection and evidence_count == 0:
        return InvestigationStatus.EMPTY

    # The model never produced a "stop" decision and we never hit a hard
    # cap — that can only happen if max_steps is 0 or some other
    # pathological misconfiguration. Treat as FAILED.
    if not stopped_by_model and not hit_step_budget and not hit_tool_budget:
        return InvestigationStatus.FAILED

    # If we hit the step or tool budget without a model stop, the
    # investigation is incomplete *regardless* of how much evidence we
    # gathered. The Teacher must know CI did not get to finish.
    if hit_step_budget or hit_tool_budget:
        return InvestigationStatus.INCOMPLETE

    # The model said stop. Now we look at what actually happened.
    if tool_calls == 0:
        # Nothing was investigated at all — not even the baseline browse.
        # This is a real failure: the system never looked.
        return InvestigationStatus.FAILED

    if failed_tool_calls > 0 and failed_tool_calls == tool_calls:
        # Every single tool call failed. We have no real evidence either
        # way. This is a partial *failure*.
        return InvestigationStatus.FAILED

    if failed_tool_calls > 0:
        # Some tools failed but some succeeded. The package reflects
        # partial work.
        return InvestigationStatus.PARTIAL

    # Tools ran cleanly. Did we end up with anything?
    if evidence_count == 0 or invalid_selection:
        return InvestigationStatus.EMPTY

    return InvestigationStatus.SUCCESSFUL


__all__ = ["ContextInvestigator"]
