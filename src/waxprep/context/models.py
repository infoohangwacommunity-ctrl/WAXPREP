"""Context Intelligence domain contracts."""

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
    """What the Teacher receives: compact, dated, and traceable."""

    wax_id: WaxId
    query: str
    evidence: tuple[Evidence, ...]
    summary: str
    uncertainty: tuple[str, ...] = ()
    investigated: tuple[str, ...] = ()
    stopped_reason: str = ""
    as_of: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


@dataclass(frozen=True, slots=True)
class InvestigationLimits:
    max_steps: int = 8
    max_tool_calls: int = 16
    max_evidence: int = 12
    max_context_characters: int = 12000
    max_model_output_tokens: int = 1000


@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    """A new student message plus the current time. No session id."""

    wax_id: WaxId
    query: str
    now: datetime
    trigger_entry_id: UUID | None = None
    limits: InvestigationLimits = field(default_factory=InvestigationLimits)
