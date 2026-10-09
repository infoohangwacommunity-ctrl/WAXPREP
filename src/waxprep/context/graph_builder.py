"""Relationship proposals — CI proposes; application validates and stores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from waxprep.domain.identifiers import WaxId
from waxprep.knowledge.models import KnowledgeEdge


@dataclass(frozen=True, slots=True)
class RelationshipProposal:
    source_node_id: UUID
    target_node_id: UUID
    relation: str
    properties: dict[str, Any]
    reason: str
    evidence_ids: tuple[UUID, ...]


def proposal_to_edge(
    proposal: RelationshipProposal,
    wax_id: WaxId,
    now: datetime,
) -> KnowledgeEdge:
    return KnowledgeEdge(
        id=uuid4(),
        wax_id=wax_id,
        source_node_id=proposal.source_node_id,
        target_node_id=proposal.target_node_id,
        relation=proposal.relation,
        properties={
            **proposal.properties,
            "proposal_reason": proposal.reason,
            "evidence_ids": [str(v) for v in proposal.evidence_ids],
        },
        valid_from=now,
        valid_until=None,
        created_at=now,
    )
