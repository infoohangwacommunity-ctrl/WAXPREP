"""Apply validated knowledge proposals (write / evolution).

Preserves history: supersession marks old nodes SUPERSEDED; it does not delete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from waxprep.context.post_interaction import (
    KnowledgeOperation,
    KnowledgeProposal,
    KnowledgeProposalValidator,
)
from waxprep.domain.models import NotebookEntry, new_id
from waxprep.knowledge.models import (
    KnowledgeEdge,
    KnowledgeEvidenceRecord,
    KnowledgeNode,
    KnowledgeNodeStatus,
)
from waxprep.knowledge.store import KnowledgeStore
from waxprep.storage.memory import InMemoryStorage


def _as_str_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[Any, Any], value).items()}


@dataclass(frozen=True, slots=True)
class EvolutionResult:
    accepted: bool
    reason: str
    proposal: KnowledgeProposal
    created_node_id: UUID | None = None
    created_edge_id: UUID | None = None
    superseded_node_id: UUID | None = None
    created_evidence_id: UUID | None = None
    notebook_entry_id: UUID | None = None


@dataclass
class KnowledgeEvolutionService:
    """Application-controlled persistence for CI proposals."""

    knowledge: KnowledgeStore
    storage: InMemoryStorage | None = None
    validator: KnowledgeProposalValidator = field(
        default_factory=KnowledgeProposalValidator
    )

    def apply(self, proposal: KnowledgeProposal, *, now: datetime) -> EvolutionResult:
        decision = self.validator.validate(proposal)
        if not decision.accepted:
            return EvolutionResult(
                accepted=False,
                reason=decision.reason,
                proposal=proposal,
            )
        op = proposal.operation
        if op == KnowledgeOperation.CREATE_NODE.value:
            return self._create_node(proposal, now)
        if op == KnowledgeOperation.CREATE_EDGE.value:
            return self._create_edge(proposal, now)
        if op == KnowledgeOperation.SUPERSEDE_NODE.value:
            return self._supersede_node(proposal, now)
        if op == KnowledgeOperation.ATTACH_EVIDENCE.value:
            return self._attach_evidence(proposal, now)
        if op == KnowledgeOperation.ADD_NOTEBOOK_ENTRY.value:
            return self._add_notebook_entry(proposal, now)
        return EvolutionResult(
            accepted=False, reason="unsupported operation", proposal=proposal
        )

    def apply_many(
        self, proposals: list[KnowledgeProposal], *, now: datetime
    ) -> list[EvolutionResult]:
        return [self.apply(p, now=now) for p in proposals]

    def _create_node(
        self, proposal: KnowledgeProposal, now: datetime
    ) -> EvolutionResult:
        p = proposal.payload
        node_id = uuid4()
        props = _as_str_dict(p.get("properties"))
        node = KnowledgeNode(
            id=node_id,
            wax_id=proposal.wax_id,
            node_type=str(p["node_type"]).strip(),
            label=str(p["label"]).strip(),
            description=(
                str(p["description"]) if p.get("description") is not None else None
            ),
            properties=props,
            status=KnowledgeNodeStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self.knowledge.put_node(node)
        return EvolutionResult(
            accepted=True,
            reason="node created",
            proposal=proposal,
            created_node_id=node_id,
        )

    def _create_edge(
        self, proposal: KnowledgeProposal, now: datetime
    ) -> EvolutionResult:
        p = proposal.payload
        try:
            source = UUID(str(p["source_node_id"]))
            target = UUID(str(p["target_node_id"]))
        except (ValueError, TypeError, KeyError):
            return EvolutionResult(
                accepted=False,
                reason="invalid source_node_id or target_node_id",
                proposal=proposal,
            )
        if self.knowledge.get_node(proposal.wax_id, source) is None:
            return EvolutionResult(
                accepted=False, reason="unknown source node", proposal=proposal
            )
        if self.knowledge.get_node(proposal.wax_id, target) is None:
            return EvolutionResult(
                accepted=False, reason="unknown target node", proposal=proposal
            )
        edge_id = uuid4()
        edge = KnowledgeEdge(
            id=edge_id,
            wax_id=proposal.wax_id,
            source_node_id=source,
            target_node_id=target,
            relation=str(p["relation"]).strip(),
            properties={
                "proposal_reason": proposal.reason,
                "evidence_ids": [str(e) for e in proposal.evidence_ids],
            },
            valid_from=now,
            valid_until=None,
            created_at=now,
        )
        self.knowledge.put_edge(edge)
        return EvolutionResult(
            accepted=True,
            reason="edge created",
            proposal=proposal,
            created_edge_id=edge_id,
        )

    def _supersede_node(
        self, proposal: KnowledgeProposal, now: datetime
    ) -> EvolutionResult:
        p = proposal.payload
        try:
            old_id = UUID(str(p["old_node_id"]))
        except (ValueError, TypeError, KeyError):
            return EvolutionResult(
                accepted=False, reason="invalid old_node_id", proposal=proposal
            )
        old = self.knowledge.get_node(proposal.wax_id, old_id)
        if old is None:
            return EvolutionResult(
                accepted=False, reason="unknown old node", proposal=proposal
            )
        superseded = KnowledgeNode(
            id=old.id,
            wax_id=old.wax_id,
            node_type=old.node_type,
            label=old.label,
            description=old.description,
            properties={**old.properties, "superseded_at": now.isoformat()},
            status=KnowledgeNodeStatus.SUPERSEDED,
            created_at=old.created_at,
            updated_at=now,
        )
        self.knowledge.put_node(superseded)

        if p.get("label") and p.get("node_type"):
            create_payload = {
                k: v
                for k, v in p.items()
                if k in ("label", "node_type", "description", "properties")
            }
            create_proposal = KnowledgeProposal(
                wax_id=proposal.wax_id,
                source_conversation_id=proposal.source_conversation_id,
                operation=KnowledgeOperation.CREATE_NODE.value,
                payload=create_payload,
                evidence_ids=proposal.evidence_ids,
                reason=proposal.reason,
            )
            created = self._create_node(create_proposal, now)
            if created.created_node_id is not None:
                self.knowledge.put_edge(
                    KnowledgeEdge(
                        id=uuid4(),
                        wax_id=proposal.wax_id,
                        source_node_id=old_id,
                        target_node_id=created.created_node_id,
                        relation="superseded_by",
                        properties={
                            "evidence_ids": [str(e) for e in proposal.evidence_ids]
                        },
                        valid_from=now,
                        valid_until=None,
                        created_at=now,
                    )
                )
            return EvolutionResult(
                accepted=True,
                reason="node superseded; history preserved",
                proposal=proposal,
                created_node_id=created.created_node_id,
                superseded_node_id=old_id,
            )
        return EvolutionResult(
            accepted=True,
            reason="node marked superseded; history preserved",
            proposal=proposal,
            superseded_node_id=old_id,
        )

    def _attach_evidence(
        self, proposal: KnowledgeProposal, now: datetime
    ) -> EvolutionResult:
        p = proposal.payload
        node_id: UUID | None = None
        edge_id: UUID | None = None
        if p.get("node_id") is not None:
            try:
                node_id = UUID(str(p["node_id"]))
            except (ValueError, TypeError):
                return EvolutionResult(
                    accepted=False, reason="invalid node_id", proposal=proposal
                )
            if self.knowledge.get_node(proposal.wax_id, node_id) is None:
                return EvolutionResult(
                    accepted=False, reason="unknown node", proposal=proposal
                )
        if p.get("edge_id") is not None:
            try:
                edge_id = UUID(str(p["edge_id"]))
            except (ValueError, TypeError):
                return EvolutionResult(
                    accepted=False, reason="invalid edge_id", proposal=proposal
                )
            if self.knowledge.edges.get((proposal.wax_id, edge_id)) is None:
                return EvolutionResult(
                    accepted=False, reason="unknown edge", proposal=proposal
                )
        source_id: UUID | None = None
        if p.get("source_id") is not None:
            try:
                source_id = UUID(str(p["source_id"]))
            except (ValueError, TypeError):
                return EvolutionResult(
                    accepted=False, reason="invalid source_id", proposal=proposal
                )
        evidence_id = uuid4()
        record = KnowledgeEvidenceRecord(
            id=evidence_id,
            wax_id=proposal.wax_id,
            node_id=node_id,
            edge_id=edge_id,
            source_type=str(p["source_type"]),
            source_id=source_id,
            quote=str(p["quote"]) if p.get("quote") is not None else None,
            metadata={"proposal_reason": proposal.reason},
            created_at=now,
        )
        self.knowledge.put_evidence(record)
        return EvolutionResult(
            accepted=True,
            reason="evidence attached",
            proposal=proposal,
            created_evidence_id=evidence_id,
        )

    def _add_notebook_entry(
        self, proposal: KnowledgeProposal, now: datetime
    ) -> EvolutionResult:
        if self.storage is None:
            return EvolutionResult(
                accepted=False,
                reason="notebook storage not configured",
                proposal=proposal,
            )
        notebook = self.storage.get_or_create_notebook(proposal.wax_id, now)
        entry_id = new_id()
        note_payload = _as_str_dict(proposal.payload.get("payload"))
        if not note_payload and not isinstance(proposal.payload.get("payload"), dict):
            return EvolutionResult(
                accepted=False, reason="invalid notebook payload", proposal=proposal
            )
        entry = NotebookEntry(
            id=entry_id,
            wax_id=proposal.wax_id,
            notebook_id=notebook.id,
            schema_version=1,
            payload=note_payload,
            source_type="context_intelligence",
            source_ref=str(proposal.source_conversation_id),
            created_at=now,
            updated_at=now,
        )
        self.storage.add_notebook_entry(entry)
        return EvolutionResult(
            accepted=True,
            reason="notebook entry added",
            proposal=proposal,
            notebook_entry_id=entry_id,
        )
