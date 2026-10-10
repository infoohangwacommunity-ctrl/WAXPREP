"""Hybrid retrieval: lexical + optional semantic, always WAX-ID scoped."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from waxprep.context.models import ContextDecision, Evidence, EvidenceType
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import Message
from waxprep.embeddings.similarity import cosine_similarity
from waxprep.knowledge.store import KnowledgeStore
from waxprep.storage.memory import InMemoryStorage


@dataclass(frozen=True, slots=True)
class RankedHit:
    evidence: Evidence
    score: float
    channel: str  # lexical | semantic | graph


def _clip(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


class HybridRetriever:
    """Combines lexical storage search with optional embedding similarity."""

    def __init__(
        self,
        storage: InMemoryStorage,
        knowledge: KnowledgeStore | None = None,
        *,
        embedding_index: list[tuple[UUID, str, tuple[float, ...]]] | None = None,
    ) -> None:
        self._storage = storage
        self._knowledge = knowledge or KnowledgeStore()
        # (object_id, content, vector) precomputed for semantic search
        self._embedding_index = embedding_index or []

    def search_conversation(
        self,
        wax_id: WaxId,
        query: str,
        *,
        limit: int = 8,
        conversation_id: UUID | None = None,
    ) -> tuple[Evidence, ...]:
        q = query.lower().strip()
        hits: list[RankedHit] = []
        for message in self._storage.list_all_messages(wax_id):
            if (
                conversation_id is not None
                and message.conversation_id != conversation_id
            ):
                continue
            body = message.text_body or ""
            if not body:
                continue
            score = _lexical_score(q, body.lower())
            if score <= 0:
                continue
            hits.append(
                RankedHit(
                    evidence=_message_evidence(wax_id, message, score),
                    score=score,
                    channel="lexical",
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return tuple(h.evidence for h in hits[:limit])

    def search_notebook(
        self, wax_id: WaxId, query: str, *, limit: int = 8
    ) -> tuple[Evidence, ...]:
        q = query.lower().strip()
        notebook = self._storage.get_notebook(wax_id)
        if notebook is None:
            return ()
        entries = self._storage.list_notebook_entries(wax_id, notebook.id)
        hits: list[RankedHit] = []
        for entry in entries:
            text = str(entry.payload)
            score = _lexical_score(q, text.lower())
            if score <= 0:
                continue
            hits.append(
                RankedHit(
                    evidence=Evidence(
                        id=entry.id,
                        wax_id=wax_id,
                        evidence_type=EvidenceType.NOTEBOOK,
                        source_id=entry.id,
                        content=_clip(text),
                        created_at=entry.created_at,
                        relevance_reason=f"lexical notebook score={score:.3f}",
                        decision=ContextDecision.NEEDS_CHECKING,
                    ),
                    score=score,
                    channel="lexical",
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return tuple(h.evidence for h in hits[:limit])

    def search_workspace(
        self, wax_id: WaxId, query: str, *, limit: int = 8
    ) -> tuple[Evidence, ...]:
        q = query.lower().strip()
        workspace = self._storage.get_workspace(wax_id)
        if workspace is None:
            return ()
        hits: list[RankedHit] = []
        for artifact in self._storage.list_artifacts(wax_id, workspace.id):
            versions = self._storage.list_artifact_versions(wax_id, artifact.id)
            for ver in versions:
                hay = " ".join(
                    filter(
                        None,
                        [
                            ver.original_filename or "",
                            ver.mime_type or "",
                            ver.storage_ref,
                        ],
                    )
                ).lower()
                score = _lexical_score(q, hay)
                if score <= 0:
                    continue
                hits.append(
                    RankedHit(
                        evidence=Evidence(
                            id=ver.id,
                            wax_id=wax_id,
                            evidence_type=EvidenceType.ARTIFACT,
                            source_id=artifact.id,
                            content=_clip(
                                f"artifact={artifact.id} file={ver.original_filename} "
                                f"mime={ver.mime_type} ref={ver.storage_ref}"
                            ),
                            created_at=ver.created_at,
                            relevance_reason=f"lexical workspace score={score:.3f}",
                            decision=ContextDecision.NEEDS_CHECKING,
                        ),
                        score=score,
                        channel="lexical",
                    )
                )
        hits.sort(key=lambda h: h.score, reverse=True)
        return tuple(h.evidence for h in hits[:limit])

    def search_knowledge(
        self, wax_id: WaxId, query: str, *, limit: int = 8
    ) -> tuple[Evidence, ...]:
        nodes = self._knowledge.search_nodes(wax_id, query, limit=limit)
        out: list[Evidence] = []
        for node in nodes:
            out.append(
                Evidence(
                    id=node.id,
                    wax_id=wax_id,
                    evidence_type=EvidenceType.KNOWLEDGE_NODE,
                    source_id=node.id,
                    content=_clip(
                        f"[{node.node_type}] {node.label}: {node.description or ''}"
                    ),
                    created_at=node.created_at,
                    relevance_reason="knowledge lexical match",
                    decision=ContextDecision.NEEDS_CHECKING,
                )
            )
        return tuple(out)

    def search_semantically(
        self,
        wax_id: WaxId,
        query_vector: tuple[float, ...],
        *,
        limit: int = 8,
    ) -> tuple[Evidence, ...]:
        """Rank pre-indexed content by cosine similarity (bounded, same student)."""
        hits: list[RankedHit] = []
        for object_id, content, vector in self._embedding_index:
            # index is assumed pre-filtered to this student by caller
            try:
                score = cosine_similarity(query_vector, vector)
            except ValueError:
                continue
            if score <= 0:
                continue
            hits.append(
                RankedHit(
                    evidence=Evidence(
                        id=object_id,
                        wax_id=wax_id,
                        evidence_type=EvidenceType.CONVERSATION,
                        source_id=object_id,
                        content=_clip(content),
                        created_at=datetime(1970, 1, 1, tzinfo=UTC),
                        relevance_reason=f"semantic cosine={score:.3f}",
                        decision=ContextDecision.NEEDS_CHECKING,
                    ),
                    score=score,
                    channel="semantic",
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return tuple(h.evidence for h in hits[:limit])

    def follow_relationships(
        self,
        wax_id: WaxId,
        node_id: UUID,
        *,
        direction: str = "both",
        limit: int = 8,
    ) -> tuple[Evidence, ...]:
        edges = self._knowledge.follow(
            wax_id, node_id, direction=direction, limit=limit
        )
        out: list[Evidence] = []
        for edge in edges:
            out.append(
                Evidence(
                    id=edge.id,
                    wax_id=wax_id,
                    evidence_type=EvidenceType.KNOWLEDGE_EDGE,
                    source_id=edge.id,
                    content=_clip(
                        f"{edge.source_node_id} -[{edge.relation}]-> "
                        f"{edge.target_node_id}"
                    ),
                    created_at=edge.created_at,
                    relevance_reason="graph traversal",
                    decision=ContextDecision.NEEDS_CHECKING,
                )
            )
        return tuple(out)

    def inspect_evidence(
        self, wax_id: WaxId, evidence_id: UUID
    ) -> tuple[Evidence, ...]:
        # messages
        for message in self._storage.list_all_messages(wax_id):
            if message.id == evidence_id:
                return (_message_evidence(wax_id, message, 1.0),)
        # knowledge nodes
        node = self._knowledge.get_node(wax_id, evidence_id)
        if node is not None:
            return (
                Evidence(
                    id=node.id,
                    wax_id=wax_id,
                    evidence_type=EvidenceType.KNOWLEDGE_NODE,
                    source_id=node.id,
                    content=_clip(
                        f"[{node.node_type}] {node.label}: {node.description or ''}"
                    ),
                    created_at=node.created_at,
                    relevance_reason="inspect node",
                    decision=ContextDecision.INCLUDE,
                ),
            )
        return ()


def _lexical_score(query: str, haystack: str) -> float:
    if not query or not haystack:
        return 0.0
    if query in haystack:
        return 1.0 + (len(query) / max(len(haystack), 1))
    tokens = [t for t in query.split() if t]
    if not tokens:
        return 0.0
    matched = sum(1 for t in tokens if t in haystack)
    return matched / len(tokens)


def _message_evidence(wax_id: WaxId, message: Message, score: float) -> Evidence:
    return Evidence(
        id=message.id,
        wax_id=wax_id,
        evidence_type=EvidenceType.CONVERSATION,
        source_id=message.id,
        content=_clip(message.text_body or ""),
        created_at=message.created_at,
        relevance_reason=f"lexical conversation score={score:.3f}",
        decision=ContextDecision.NEEDS_CHECKING,
    )
