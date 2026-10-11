"""Turning stored objects into dated, attributed evidence."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from waxprep.context.models import ContextDecision, Evidence, EvidenceType
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    NotebookEntry,
    NotebookEntryStatus,
    ObjectType,
    Summary,
    SummaryStatus,
)
from waxprep.index.objects import flatten_payload
from waxprep.knowledge.models import KnowledgeNode, KnowledgeNodeStatus
from waxprep.storage.protocol import Storage

MAX_CONTENT_CHARS = 2000


def clip(text: str, max_chars: int = MAX_CONTENT_CHARS) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def history_evidence(entry: HistoryEntry, reason: str) -> Evidence:
    if entry.author_kind is AuthorKind.STUDENT:
        note = "said by the student"
    elif entry.author_kind is AuthorKind.TUTOR:
        note = "said by the tutor (not proof of any fact about the student)"
    else:
        note = "system record"
    return Evidence(
        id=entry.id,
        wax_id=entry.wax_id,
        evidence_type=EvidenceType.HISTORY,
        source_id=entry.id,
        content=clip(entry.text or ""),
        created_at=entry.occurred_at,
        relevance_reason=reason,
        decision=ContextDecision.NEEDS_CHECKING,
        author_kind=entry.author_kind.value,
        provenance=(note,),
    )


def summary_evidence(summary: Summary, reason: str) -> Evidence:
    superseded = summary.status is SummaryStatus.SUPERSEDED
    return Evidence(
        id=summary.id,
        wax_id=summary.wax_id,
        evidence_type=EvidenceType.SUMMARY,
        source_id=summary.id,
        content=clip(summary.text),
        created_at=summary.covers_until,
        relevance_reason=reason,
        decision=(
            ContextDecision.SUPERSEDED if superseded else ContextDecision.NEEDS_CHECKING
        ),
        provenance=(
            f"compressed summary of history from {summary.covers_from.isoformat()} "
            f"to {summary.covers_until.isoformat()}",
        ),
    )


def notebook_evidence(entry: NotebookEntry, reason: str) -> Evidence:
    superseded = entry.status is NotebookEntryStatus.SUPERSEDED
    return Evidence(
        id=entry.id,
        wax_id=entry.wax_id,
        evidence_type=EvidenceType.NOTEBOOK,
        source_id=entry.id,
        content=clip(flatten_payload(entry.payload)),
        created_at=entry.created_at,
        relevance_reason=reason,
        decision=(
            ContextDecision.SUPERSEDED if superseded else ContextDecision.NEEDS_CHECKING
        ),
        provenance=("durable understanding derived from history",),
    )


def node_evidence(node: KnowledgeNode, reason: str) -> Evidence:
    superseded = node.status is KnowledgeNodeStatus.SUPERSEDED
    return Evidence(
        id=node.id,
        wax_id=node.wax_id,
        evidence_type=EvidenceType.KNOWLEDGE_NODE,
        source_id=node.id,
        content=clip(f"[{node.node_type}] {node.label}: {node.description or ''}"),
        created_at=node.created_at,
        relevance_reason=reason,
        decision=(
            ContextDecision.SUPERSEDED if superseded else ContextDecision.NEEDS_CHECKING
        ),
        provenance=("relationship understanding derived from history",),
    )


def hydrate(
    storage: Storage,
    wax_id: WaxId,
    object_type: ObjectType,
    object_id: UUID,
    reason: str,
) -> Evidence | None:
    """Load any stored object as evidence, or None when it does not exist."""
    if object_type is ObjectType.HISTORY:
        entry = storage.get_history_entry(wax_id, object_id)
        return history_evidence(entry, reason) if entry else None
    if object_type is ObjectType.SUMMARY:
        summary = storage.get_summary(wax_id, object_id)
        return summary_evidence(summary, reason) if summary else None
    if object_type is ObjectType.NOTEBOOK:
        note = storage.get_notebook_entry(wax_id, object_id)
        return notebook_evidence(note, reason) if note else None
    if object_type is ObjectType.KNOWLEDGE_NODE:
        node = storage.get_node(wax_id, object_id)
        return node_evidence(node, reason) if node else None
    if object_type is ObjectType.ARTIFACT_VERSION:
        version = storage.get_artifact_version_by_id(wax_id, object_id)
        if version is None:
            return None
        text = (
            f"artifact={version.artifact_id} version={version.version_number} "
            f"file={version.original_filename} mime={version.mime_type}"
        )
        return Evidence(
            id=version.id,
            wax_id=wax_id,
            evidence_type=EvidenceType.ARTIFACT,
            source_id=version.artifact_id,
            content=clip(text),
            created_at=version.created_at,
            relevance_reason=reason,
            decision=ContextDecision.NEEDS_CHECKING,
            provenance=("stored workspace artifact",),
        )
    if object_type is ObjectType.ARTIFACT_CONTENT:
        content = storage.get_artifact_content(wax_id, object_id)
        if content is None:
            return None
        return Evidence(
            id=content.id,
            wax_id=wax_id,
            evidence_type=EvidenceType.ARTIFACT_CONTENT,
            source_id=content.artifact_version_id,
            content=clip(content.text),
            created_at=content.created_at,
            relevance_reason=reason,
            decision=ContextDecision.NEEDS_CHECKING,
            provenance=(
                f"{content.representation} extracted from an artifact "
                f"by {content.produced_by}",
            ),
        )
    return None


def format_for_model(evidence: list[Evidence], now: datetime, max_chars: int) -> str:
    """Render evidence for the CI model, always with date, age, and author."""
    from waxprep.context.temporal import describe_age

    if not evidence:
        return "(no evidence yet)"
    parts: list[str] = []
    total = 0
    for item in evidence:
        who = f" by {item.author_kind}" if item.author_kind else ""
        notes = "; ".join(item.provenance)
        chunk = (
            f"[{item.id}] ({item.evidence_type.value}{who}) "
            f"{item.created_at.isoformat()} ({describe_age(now, item.created_at)}) "
            f"[{item.decision.value}] {item.content}" + (f" | {notes}" if notes else "")
        )
        if total + len(chunk) > max_chars:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)
