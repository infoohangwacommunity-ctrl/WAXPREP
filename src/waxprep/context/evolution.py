"""Applying validated proposals to the student's derived understanding.

History is never touched here. Derived objects (notebook, knowledge, summaries)
are created, replaced, and linked back to the evidence that supports them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from waxprep.context.evidence import hydrate
from waxprep.context.proposals import (
    Operation,
    Proposal,
    ProposalValidator,
)
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    ArtifactReference,
    AuthorKind,
    NotebookEntry,
    ObjectType,
    ProvenanceLink,
    Summary,
    new_id,
)
from waxprep.index.indexer import Indexer
from waxprep.index.objects import flatten_payload
from waxprep.knowledge.models import KnowledgeEdge, KnowledgeNode, KnowledgeNodeStatus
from waxprep.storage.protocol import Storage

NEEDS_CONFIRMATION = "needs_student_confirmation"
LINKABLE = {
    ObjectType.HISTORY.value: ObjectType.HISTORY,
    ObjectType.SUMMARY.value: ObjectType.SUMMARY,
    ObjectType.NOTEBOOK.value: ObjectType.NOTEBOOK,
    ObjectType.KNOWLEDGE_NODE.value: ObjectType.KNOWLEDGE_NODE,
}

# Blocker G: bound recursive provenance resolution. A pathological graph
# could otherwise loop forever (a summary citing a notebook citing the
# summary). 10 levels is more than enough for any realistic derivation
# chain (history → summary → summary-of-summary → notebook).
_MAX_PROVENANCE_DEPTH = 10


def _str_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[Any, Any], value).items()}


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class EvolutionResult:
    accepted: bool
    reason: str
    proposal: Proposal
    created_node_id: UUID | None = None
    created_edge_id: UUID | None = None
    superseded_id: UUID | None = None
    created_notebook_entry_id: UUID | None = None
    created_summary_id: UUID | None = None
    links_added: int = 0


@dataclass
class EvolutionService:
    """Application-controlled persistence for Context Intelligence proposals."""

    storage: Storage
    indexer: Indexer
    validator: ProposalValidator = field(default_factory=ProposalValidator)

    def apply(self, proposal: Proposal, *, now: datetime) -> EvolutionResult:
        decision = self.validator.validate(proposal)
        if not decision.accepted:
            return EvolutionResult(False, decision.reason, proposal)
        missing = self._first_missing_evidence(proposal)
        if missing is not None:
            return EvolutionResult(False, missing, proposal)
        handlers = {
            Operation.CREATE_NODE.value: self._create_node,
            Operation.CREATE_EDGE.value: self._create_edge,
            Operation.SUPERSEDE_NODE.value: self._supersede_node,
            Operation.ATTACH_EVIDENCE.value: self._attach_evidence,
            Operation.ADD_NOTEBOOK_ENTRY.value: self._add_notebook_entry,
            Operation.SUPERSEDE_NOTEBOOK_ENTRY.value: self._supersede_notebook_entry,
            Operation.SUPERSEDE_SUMMARY.value: self._supersede_summary,
            Operation.LINK_ARTIFACT.value: self._link_artifact,
        }
        # Spec section 9: derived-object + provenance + index writes
        # must be atomic. A process crash between the derived-object save
        # and the provenance write must not leave an orphan.
        with self.storage.transaction():
            return handlers[proposal.operation](proposal, now)

    # --- evidence and provenance ---------------------------------------------
    def _first_missing_evidence(self, proposal: Proposal) -> str | None:
        for ref in proposal.evidence:
            found = hydrate(
                self.storage, proposal.wax_id, ref.object_type, ref.object_id, "check"
            )
            if found is None:
                return f"unknown evidence {ref.object_type.value}:{ref.object_id}"
        return None

    def _author_of(
        self, wax_id: WaxId, ref_type: ObjectType, ref_id: UUID
    ) -> AuthorKind | None:
        if ref_type is not ObjectType.HISTORY:
            return None
        entry = self.storage.get_history_entry(wax_id, ref_id)
        return entry.author_kind if entry else None

    def _needs_confirmation(self, proposal: Proposal) -> bool:
        """True when the only support is things the tutor itself said.

        Blocker G (spec 8.2-G): the original logic only checked immediate
        history evidence. A notebook entry supported by a summary whose
        entire lineage came from tutor-only messages would lose the
        "needs student confirmation" flag. Resolve provenance recursively
        through summaries and notebook entries back to original source
        messages, with cycle/depth protection.
        """
        if not proposal.evidence:
            return True
        # Collect every reachable history entry through the evidence graph.
        reachable_authors: list[AuthorKind] = []
        for ref in proposal.evidence:
            reachable_authors.extend(
                self._resolve_to_history_authors(
                    proposal.wax_id, ref.object_type, ref.object_id
                )
            )
        if not reachable_authors:
            # No history reach: needs confirmation (or the evidence is invalid,
            # which `_first_missing_evidence` already rejected).
            return True
        return AuthorKind.STUDENT not in reachable_authors

    def _resolve_to_history_authors(
        self,
        wax_id: WaxId,
        derived_type: ObjectType,
        derived_id: UUID,
        *,
        _seen: set[tuple[ObjectType, UUID]] | None = None,
        _depth: int = 0,
    ) -> list[AuthorKind]:
        """Recursively trace provenance back to history entries.

        Returns the author_kind of every reachable history entry. Cycle-
        protected via `_seen` and bounded by `_MAX_PROVENANCE_DEPTH`.
        """
        if _depth > _MAX_PROVENANCE_DEPTH:
            return []
        seen = _seen or set()
        key = (derived_type, derived_id)
        if key in seen:
            return []
        seen.add(key)
        # Base case: the object itself is a history entry.
        if derived_type is ObjectType.HISTORY:
            entry = self.storage.get_history_entry(wax_id, derived_id)
            return [entry.author_kind] if entry else []
        # Recursive case: trace provenance links back to sources.
        links = self.storage.list_provenance(wax_id, derived_type, derived_id)
        authors: list[AuthorKind] = []
        for link in links:
            authors.extend(
                self._resolve_to_history_authors(
                    wax_id,
                    link.source_type,
                    link.source_id,
                    _seen=seen,
                    _depth=_depth + 1,
                )
            )
        return authors

    def _link(
        self,
        proposal: Proposal,
        derived_type: ObjectType,
        derived_id: UUID,
        now: datetime,
    ) -> int:
        links = tuple(
            ProvenanceLink(
                id=new_id(),
                wax_id=proposal.wax_id,
                derived_type=derived_type,
                derived_id=derived_id,
                source_type=ref.object_type,
                source_id=ref.object_id,
                created_at=now,
                source_author_kind=self._author_of(
                    proposal.wax_id, ref.object_type, ref.object_id
                ),
                quote=ref.quote,
            )
            for ref in proposal.evidence
        )
        self.storage.add_provenance_links(links)
        return len(links)

    # --- operations ----------------------------------------------------------------
    def _new_node(
        self, proposal: Proposal, p: dict[str, Any], now: datetime
    ) -> KnowledgeNode:
        props = _str_dict(p.get("properties"))
        if self._needs_confirmation(proposal):
            props[NEEDS_CONFIRMATION] = True
        description = p.get("description")
        node = KnowledgeNode(
            id=new_id(),
            wax_id=proposal.wax_id,
            node_type=str(p["node_type"]).strip(),
            label=str(p["label"]).strip(),
            description=str(description) if description is not None else None,
            properties=props,
            status=KnowledgeNodeStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self.storage.save_node(node)
        self._link(proposal, ObjectType.KNOWLEDGE_NODE, node.id, now)
        self.indexer.index(
            proposal.wax_id,
            ObjectType.KNOWLEDGE_NODE,
            node.id,
            f"{node.node_type} {node.label} {node.description or ''}",
            now,
        )
        return node

    def _create_node(self, proposal: Proposal, now: datetime) -> EvolutionResult:
        node = self._new_node(proposal, proposal.payload, now)
        return EvolutionResult(True, "node created", proposal, created_node_id=node.id)

    def _create_edge(self, proposal: Proposal, now: datetime) -> EvolutionResult:
        p = proposal.payload
        source, target = _uuid(p["source_node_id"]), _uuid(p["target_node_id"])
        if source is None or target is None:
            return EvolutionResult(
                False, "invalid source_node_id or target_node_id", proposal
            )
        if self.storage.get_node(proposal.wax_id, source) is None:
            return EvolutionResult(False, "unknown source node", proposal)
        if self.storage.get_node(proposal.wax_id, target) is None:
            return EvolutionResult(False, "unknown target node", proposal)
        edge = KnowledgeEdge(
            id=new_id(),
            wax_id=proposal.wax_id,
            source_node_id=source,
            target_node_id=target,
            relation=str(p["relation"]).strip(),
            properties={"proposal_reason": proposal.reason},
            valid_from=now,
            valid_until=None,
            created_at=now,
        )
        self.storage.add_edge(edge)
        self._link(proposal, ObjectType.KNOWLEDGE_EDGE, edge.id, now)
        return EvolutionResult(True, "edge created", proposal, created_edge_id=edge.id)

    def _supersede_node(self, proposal: Proposal, now: datetime) -> EvolutionResult:
        p = proposal.payload
        old_id = _uuid(p["old_node_id"])
        old = self.storage.get_node(proposal.wax_id, old_id) if old_id else None
        if old is None:
            return EvolutionResult(False, "unknown old node", proposal)
        self.storage.save_node(
            replace(
                old,
                status=KnowledgeNodeStatus.SUPERSEDED,
                properties={**old.properties, "superseded_at": now.isoformat()},
                updated_at=now,
            )
        )
        if not (p.get("label") and p.get("node_type")):
            return EvolutionResult(
                True,
                "node marked superseded; history preserved",
                proposal,
                superseded_id=old.id,
            )
        new_node = self._new_node(proposal, p, now)
        edge = KnowledgeEdge(
            id=new_id(),
            wax_id=proposal.wax_id,
            source_node_id=old.id,
            target_node_id=new_node.id,
            relation="superseded_by",
            properties={},
            valid_from=now,
            valid_until=None,
            created_at=now,
        )
        self.storage.add_edge(edge)
        self._link(proposal, ObjectType.KNOWLEDGE_EDGE, edge.id, now)
        return EvolutionResult(
            True,
            "node superseded; history preserved",
            proposal,
            created_node_id=new_node.id,
            created_edge_id=edge.id,
            superseded_id=old.id,
        )

    def _attach_evidence(self, proposal: Proposal, now: datetime) -> EvolutionResult:
        p = proposal.payload
        derived_id = _uuid(p["derived_id"])
        try:
            derived_type = ObjectType(str(p["derived_type"]))
        except ValueError:
            return EvolutionResult(False, "unknown derived_type", proposal)
        if derived_id is None:
            return EvolutionResult(False, "invalid derived_id", proposal)
        if derived_type is ObjectType.KNOWLEDGE_EDGE:
            exists = self.storage.get_edge(proposal.wax_id, derived_id) is not None
        else:
            exists = (
                hydrate(
                    self.storage, proposal.wax_id, derived_type, derived_id, "check"
                )
                is not None
            )
        if not exists:
            return EvolutionResult(False, "unknown derived object", proposal)
        added = self._link(proposal, derived_type, derived_id, now)
        return EvolutionResult(True, "evidence attached", proposal, links_added=added)

    def _notebook_entry(self, proposal: Proposal, now: datetime) -> NotebookEntry:
        notebook = self.storage.get_or_create_notebook(proposal.wax_id, now)
        payload = _str_dict(proposal.payload.get("payload"))
        if self._needs_confirmation(proposal):
            payload[NEEDS_CONFIRMATION] = True
        entry = NotebookEntry(
            id=new_id(),
            wax_id=proposal.wax_id,
            notebook_id=notebook.id,
            schema_version=1,
            payload=payload,
            created_at=now,
            updated_at=now,
        )
        self.storage.add_notebook_entry(entry)
        self._link(proposal, ObjectType.NOTEBOOK, entry.id, now)
        self.indexer.index(
            proposal.wax_id,
            ObjectType.NOTEBOOK,
            entry.id,
            flatten_payload(payload),
            now,
        )
        return entry

    def _add_notebook_entry(self, proposal: Proposal, now: datetime) -> EvolutionResult:
        entry = self._notebook_entry(proposal, now)
        return EvolutionResult(
            True, "notebook entry added", proposal, created_notebook_entry_id=entry.id
        )

    def _supersede_notebook_entry(
        self, proposal: Proposal, now: datetime
    ) -> EvolutionResult:
        old_id = _uuid(proposal.payload["old_entry_id"])
        old = (
            self.storage.get_notebook_entry(proposal.wax_id, old_id) if old_id else None
        )
        if old is None:
            return EvolutionResult(False, "unknown old notebook entry", proposal)
        entry = self._notebook_entry(proposal, now)
        self.storage.supersede_notebook_entry(proposal.wax_id, old.id, entry.id, now)
        return EvolutionResult(
            True,
            "notebook entry replaced; earlier entry and its evidence preserved",
            proposal,
            created_notebook_entry_id=entry.id,
            superseded_id=old.id,
        )

    def _supersede_summary(self, proposal: Proposal, now: datetime) -> EvolutionResult:
        p = proposal.payload
        old_id = _uuid(p["old_summary_id"])
        old = self.storage.get_summary(proposal.wax_id, old_id) if old_id else None
        if old is None:
            return EvolutionResult(False, "unknown old summary", proposal)
        fresh = Summary(
            id=new_id(),
            wax_id=proposal.wax_id,
            text=str(p["text"]).strip(),
            covers_from=old.covers_from,
            covers_until=old.covers_until,
            level=old.level,
            produced_by="context_intelligence",
            created_at=now,
        )
        self.storage.add_summary(fresh)
        self.storage.supersede_summary(proposal.wax_id, old.id, now)
        self._link(proposal, ObjectType.SUMMARY, fresh.id, now)
        self.indexer.index(
            proposal.wax_id,
            ObjectType.SUMMARY,
            fresh.id,
            fresh.text,
            fresh.covers_until,
        )
        return EvolutionResult(
            True,
            "summary refreshed",
            proposal,
            created_summary_id=fresh.id,
            superseded_id=old.id,
        )

    def _link_artifact(self, proposal: Proposal, now: datetime) -> EvolutionResult:
        p = proposal.payload
        artifact_id, ref_id = _uuid(p["artifact_id"]), _uuid(p["reference_id"])
        ref_type = LINKABLE.get(str(p["reference_type"]))
        if artifact_id is None or ref_id is None or ref_type is None:
            return EvolutionResult(False, "invalid artifact link", proposal)
        if self.storage.get_artifact(proposal.wax_id, artifact_id) is None:
            return EvolutionResult(False, "unknown artifact", proposal)
        if hydrate(self.storage, proposal.wax_id, ref_type, ref_id, "check") is None:
            return EvolutionResult(False, "unknown reference target", proposal)
        self.storage.create_artifact_reference(
            ArtifactReference(
                id=new_id(),
                wax_id=proposal.wax_id,
                artifact_id=artifact_id,
                reference_type=ref_type.value,
                reference_id=ref_id,
                created_at=now,
            )
        )
        return EvolutionResult(True, "artifact linked", proposal)
