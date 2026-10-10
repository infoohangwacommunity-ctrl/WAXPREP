"""In-memory knowledge store for CI read tools (open-world, student-scoped)."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.knowledge.models import (
    KnowledgeEdge,
    KnowledgeEvidenceRecord,
    KnowledgeNode,
    KnowledgeNodeStatus,
)

EmbeddingEntry = tuple[str, tuple[float, ...], str]


@dataclass
class KnowledgeStore:
    """Deterministic knowledge graph storage for offline tests and tools."""

    nodes: dict[tuple[WaxId, UUID], KnowledgeNode] = field(
        default_factory=lambda: dict[tuple[WaxId, UUID], KnowledgeNode]()
    )
    edges: dict[tuple[WaxId, UUID], KnowledgeEdge] = field(
        default_factory=lambda: dict[tuple[WaxId, UUID], KnowledgeEdge]()
    )
    evidence: dict[tuple[WaxId, UUID], KnowledgeEvidenceRecord] = field(
        default_factory=lambda: dict[tuple[WaxId, UUID], KnowledgeEvidenceRecord]()
    )
    embeddings: dict[tuple[WaxId, UUID], EmbeddingEntry] = field(
        default_factory=lambda: dict[tuple[WaxId, UUID], EmbeddingEntry]()
    )

    def put_node(self, node: KnowledgeNode) -> KnowledgeNode:
        self.nodes[(node.wax_id, node.id)] = node
        return node

    def get_node(self, wax_id: WaxId, node_id: UUID) -> KnowledgeNode | None:
        return self.nodes.get((wax_id, node_id))

    def search_nodes(
        self, wax_id: WaxId, query: str, limit: int = 8
    ) -> tuple[KnowledgeNode, ...]:
        q = query.lower().strip()
        hits = [
            n
            for (w, _), n in self.nodes.items()
            if w == wax_id
            and n.status is KnowledgeNodeStatus.ACTIVE
            and (
                q in n.label.lower()
                or q in n.node_type.lower()
                or (n.description and q in n.description.lower())
            )
        ]
        return tuple(hits[:limit])

    def put_edge(self, edge: KnowledgeEdge) -> KnowledgeEdge:
        self.edges[(edge.wax_id, edge.id)] = edge
        return edge

    def follow(
        self,
        wax_id: WaxId,
        node_id: UUID,
        *,
        direction: str = "both",
        limit: int = 8,
    ) -> tuple[KnowledgeEdge, ...]:
        results: list[KnowledgeEdge] = []
        for (w, _), e in self.edges.items():
            if w != wax_id:
                continue
            if direction in ("outgoing", "both") and e.source_node_id == node_id:
                results.append(e)
            elif direction in ("incoming", "both") and e.target_node_id == node_id:
                results.append(e)
        return tuple(results[:limit])

    def put_evidence(self, record: KnowledgeEvidenceRecord) -> KnowledgeEvidenceRecord:
        self.evidence[(record.wax_id, record.id)] = record
        return record

    def evidence_for_node(
        self, wax_id: WaxId, node_id: UUID
    ) -> tuple[KnowledgeEvidenceRecord, ...]:
        return tuple(
            r
            for (w, _), r in self.evidence.items()
            if w == wax_id and r.node_id == node_id
        )

    def put_embedding(
        self,
        wax_id: WaxId,
        object_id: UUID,
        object_type: str,
        vector: tuple[float, ...],
        model: str,
    ) -> None:
        self.embeddings[(wax_id, object_id)] = (object_type, vector, model)

    def list_embeddings(
        self, wax_id: WaxId
    ) -> list[tuple[UUID, str, tuple[float, ...], str]]:
        out: list[tuple[UUID, str, tuple[float, ...], str]] = []
        for (w, oid), (otype, vec, model) in self.embeddings.items():
            if w == wax_id:
                out.append((oid, otype, vec, model))
        return out
