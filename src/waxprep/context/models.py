"""Context Intelligence domain contracts.

The contracts here are the boundary between the investigation runtime
(``ContextInvestigator``) and the rest of the system. Three things matter:

1. ``ContextPackage`` is an *evidence contract*, not just a summary. It
   carries an explicit ``investigation_status`` so the Teacher can
   distinguish "CI looked and found nothing" from "CI never finished".

2. ``Evidence`` is always dated, attributed, and provenance-linked.

3. ``InvestigationLimits`` are mechanical budgets — they bound what the
   runtime does, not what the model decides to ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from waxprep.domain.identifiers import WaxId


class ContextDecision(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    INCLUDE_WITH_CAUTION = "include_with_caution"
    NEEDS_CHECKING = "needs_checking"
    SUPERSEDED = "superseded"
    IRRELEVANT = "irrelevant"


class EvidenceType(StrEnum):
    HISTORY = "history"
    SUMMARY = "summary"
    NOTEBOOK = "notebook"
    ARTIFACT = "artifact"
    ARTIFACT_CONTENT = "artifact_content"
    KNOWLEDGE_NODE = "knowledge_node"
    KNOWLEDGE_EDGE = "knowledge_edge"


class InvestigationStatus(StrEnum):
    """Explicit lifecycle state of one Context Intelligence investigation.

    The status is computed by the runtime from what actually happened — it
    is never just the model's own claim that it is "done". This lets the
    Teacher (and any operator reading the audit log) distinguish a successful
    investigation from one that hit a hard limit, had tools fail, or never
    got off the ground.

    The closed set is the contract. Do not invent new values ad hoc: each new
    value must correspond to a documented failure mode so the Teacher can
    reason about what it is being told.
    """

    SUCCESSFUL = "successful"
    """Investigation completed and at least one piece of relevant evidence
    was gathered. The package may still carry uncertainty, but the
    investigation itself is sound."""

    EMPTY = "empty"
    """Investigation completed (tools ran, the model finalised) but no
    evidence was returned. The Teacher should not invent context; the
    package accurately represents that nothing relevant was found."""

    INCOMPLETE = "incomplete"
    """The investigation hit a hard mechanical limit (max steps, max tool
    calls, max evidence, or a model timeout) before the model signalled it
    was done. The package reflects partial work; the Teacher should treat
    missing context as genuinely unknown."""

    PARTIAL = "partial"
    """At least one tool failed or was unavailable during the investigation.
    The package reflects what survived. Distinct from INCOMPLETE because the
    cause is tool failure, not budget exhaustion."""

    FAILED = "failed"
    """The investigation itself broke before any reliable result could be
    produced (e.g. the model provider was unavailable, every step failed,
    or the runtime could not assemble a final package). The package is
    best-effort and should be treated as 'no context' by the Teacher."""

    SKIPPED = "skipped"
    """No investigation was attempted. Used for blank or whitespace-only
    messages where there is nothing to investigate. Distinct from EMPTY
    because the runtime never even looped."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One piece of evidence, always carrying when it happened and who said it."""

    id: UUID
    wax_id: WaxId
    evidence_type: EvidenceType
    source_id: UUID
    content: str
    created_at: datetime
    relevance_reason: str
    decision: ContextDecision
    confidence_note: str | None = None
    author_kind: str | None = None
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextPackage:
    """What the Teacher receives: compact, dated, traceable, and honest.

    The package is an *evidence contract*. Its fields exist to make the
    boundary between CI and the Teacher explicit:

    - ``evidence`` is the curated set of items the model selected.
    - ``summary`` is a concise synthesis — never the whole history.
    - ``uncertainty`` lists unresolved conflicts, stale signals, or gaps.
    - ``investigated`` is an audit trail of every action attempted,
      including failures. It is the runtime's own record, not the model's.
    - ``investigation_status`` tells the Teacher, in closed vocabulary,
      what *kind* of result this is (successful / empty / incomplete /
      partial / failed / skipped). The Teacher can branch on this without
      parsing the summary text.
    - ``stopped_reason`` is a free-form short string for the audit log;
      it complements the closed-vocabulary status with a human-readable
      cause. It is *not* the source of truth — the status is.
    - ``invalid_selection`` records whether the model returned evidence
      ids that matched nothing. The runtime does NOT silently include
      everything when this happens; the failure is made observable.
    - ``metadata`` carries optional structured provenance links and other
      typed extras the renderer or auditor may want.
    """

    wax_id: WaxId
    query: str
    evidence: tuple[Evidence, ...]
    summary: str
    uncertainty: tuple[str, ...] = ()
    investigated: tuple[str, ...] = ()
    stopped_reason: str = ""
    as_of: datetime | None = None
    investigation_status: InvestigationStatus = InvestigationStatus.SUCCESSFUL
    invalid_selection: bool = False
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


@dataclass(frozen=True, slots=True)
class InvestigationLimits:
    """Mechanical bounds on one investigation. Never semantic.

    These exist to prevent runaway work, not to predict the student's
    needs. Hitting a limit is a *mechanical* event and maps to the
    ``INCOMPLETE`` investigation status. It is never treated as success.
    """

    max_steps: int = 8
    max_tool_calls: int = 16
    max_evidence: int = 12
    max_context_characters: int = 12000
    max_model_output_tokens: int = 1000


@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    """A new student message plus the current time. No session id.

    The ``trigger_entry_id`` is the durable id of the saved student
    history entry that triggered the investigation. CI uses it for
    read-around calls when it needs to inspect the context around the
    trigger itself. The application supplies it; the model does not.
    """

    wax_id: WaxId
    query: str
    now: datetime
    trigger_entry_id: UUID | None = None
    limits: InvestigationLimits = field(default_factory=InvestigationLimits)


__all__ = [
    "ContextDecision",
    "ContextPackage",
    "Evidence",
    "EvidenceType",
    "InvestigationLimits",
    "InvestigationRequest",
    "InvestigationStatus",
]
