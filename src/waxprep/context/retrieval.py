"""Retrieval capabilities for Context Intelligence.

Each method is one *capability* (exact words, meaning, time, neighbours,
sources, relationships). The model decides which to use; this module only
executes, always scoped to one WAX ID, and never decides relevance itself.

Retrieval architecture (spec section 7):
- Candidate generation and ranking are **separate concepts**.
- ``find_exact`` paginates through **all** matching token rows (no
  arbitrary wall), scoring each page and keeping a running top-N.
- ``find_by_meaning`` paginates through embeddings with a configurable
  page size, scoring each page. Early termination when the top-N is
  provably stable (no future page can improve the result).
- Pagination is deterministic (cursor = last row's
  ``(object_time, object_id, [token_hash])``).
- The design is replaceable: a real vector index (pgvector, dedicated
  service) can later be introduced without changing the memory
  architecture — only the storage implementation changes.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from waxprep.context.evidence import clip, history_evidence, hydrate
from waxprep.context.models import ContextDecision, Evidence, EvidenceType
from waxprep.context.temporal import recency_weight
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import INDEXED_OBJECT_TYPES, ObjectType
from waxprep.embeddings.similarity import cosine_similarity
from waxprep.index.tokens import TokenIndex
from waxprep.storage.protocol import Storage

ALL_INDEXED: tuple[ObjectType, ...] = tuple(sorted(INDEXED_OBJECT_TYPES, key=str))
COMMON_TOKEN_SHARE = 0.2

# Default page sizes for paginated retrieval. These are NOT memory walls —
# they bound the size of one database round-trip. The retriever continues
# paginating until either the corpus is exhausted or the top-N is stable.
DEFAULT_TOKEN_PAGE_SIZE = 5000
DEFAULT_EMBEDDING_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class Scored:
    object_type: ObjectType
    object_id: UUID
    object_time: datetime
    score: float


class Retriever:
    """Student-scoped retrieval over one unified world."""

    def __init__(
        self,
        storage: Storage,
        token_index: TokenIndex,
        *,
        recency_half_life_days: float = 180.0,
        token_page_size: int = DEFAULT_TOKEN_PAGE_SIZE,
        embedding_page_size: int = DEFAULT_EMBEDDING_PAGE_SIZE,
    ) -> None:
        self._storage = storage
        self._token_index = token_index
        self._half_life = recency_half_life_days
        self._token_page_size = token_page_size
        self._embedding_page_size = embedding_page_size

    # --- exact words -----------------------------------------------------
    def find_exact(
        self,
        wax_id: WaxId,
        query: str,
        *,
        now: datetime,
        object_types: tuple[ObjectType, ...] = ALL_INDEXED,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 8,
        ignore_recency: bool = False,
    ) -> tuple[Evidence, ...]:
        """Rank-then-limit lexical search.

        Paginates through **all** matching token rows, scoring each
        page and keeping a running top-N. There is no arbitrary wall:
        an old relevant record beyond the first page can still surface.
        """
        hashes = self._token_index.hashes(wax_id, query)
        if not hashes:
            return ()
        stats = self._storage.token_stats(wax_id, hashes, object_types)
        total = max(stats.total_objects, 1)
        weights = {
            h: math.log((total + 1) / (stats.document_frequency.get(h, 0) + 1)) + 0.1
            for h in hashes
        }
        rare = tuple(
            h
            for h in hashes
            if stats.document_frequency.get(h, 0) / total <= COMMON_TOKEN_SHARE
        )
        usable = rare or hashes

        scores: dict[tuple[ObjectType, UUID], float] = defaultdict(float)
        times: dict[tuple[ObjectType, UUID], datetime] = {}

        # Paginated retrieval: continue until the corpus is exhausted.
        cursor: tuple[datetime, UUID, bytes] | None = None
        pages = 0
        max_pages = 1000  # safety bound; one student's corpus is bounded
        while pages < max_pages:
            page = self._storage.find_by_tokens_page(
                wax_id,
                usable,
                object_types=object_types,
                since=since,
                until=until,
                page_size=self._token_page_size,
                cursor=cursor,
            )
            if not page:
                break
            for match in page:
                key = (match.object_type, match.object_id)
                scores[key] += weights.get(match.token_hash, 0.1)
                times[key] = match.object_time
            last = page[-1]
            cursor = (last.object_time, last.object_id, last.token_hash)
            pages += 1
            if len(page) < self._token_page_size:
                break  # last page

        scored = [
            Scored(
                object_type=key[0],
                object_id=key[1],
                object_time=times[key],
                score=value
                * (
                    1.0
                    if ignore_recency
                    else recency_weight(now, times[key], self._half_life)
                ),
            )
            for key, value in scores.items()
        ]
        # Rank BEFORE applying the final evidence limit — this is the
        # "rank-then-limit" property the spec requires.
        scored.sort(key=lambda s: s.score, reverse=True)
        return self._hydrate(wax_id, scored, "exact words", limit)

    # --- meaning -------------------------------------------------------------
    def find_by_meaning(
        self,
        wax_id: WaxId,
        query_vector: tuple[float, ...],
        *,
        now: datetime,
        object_types: tuple[ObjectType, ...] = ALL_INDEXED,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 8,
        ignore_recency: bool = False,
    ) -> tuple[Evidence, ...]:
        """Rank-then-limit semantic search with paginated candidate
        generation.

        Paginates through embeddings in deterministic order. Each page is
        decrypted, scored against the query vector, and the running top-N
        is kept. Early termination when the top-N's minimum score exceeds
        the max possible score from any future page (which is bounded by
        1.0 × recency_weight at the oldest possible time).
        """
        scored: list[Scored] = []
        cursor: tuple[datetime, UUID] | None = None
        pages = 0
        max_pages = 10000  # safety bound
        while pages < max_pages:
            page = self._storage.list_embeddings_page(
                wax_id,
                object_types,
                since=since,
                until=until,
                page_size=self._embedding_page_size,
                cursor=cursor,
            )
            if not page:
                break
            for record in page:
                try:
                    similarity = cosine_similarity(query_vector, record.vector)
                except ValueError:
                    continue  # vector from a different embedding model
                if similarity <= 0:
                    continue
                weight = (
                    1.0
                    if ignore_recency
                    else recency_weight(now, record.object_time, self._half_life)
                )
                scored.append(
                    Scored(
                        record.object_type,
                        record.object_id,
                        record.object_time,
                        similarity * weight,
                    )
                )
            last = page[-1]
            cursor = (last.object_time, last.object_id)
            pages += 1
            if len(page) < self._embedding_page_size:
                break  # last page
            # Early termination: if the top-N's min score already exceeds
            # the max possible score from any future page, stop. The max
            # possible score for any future embedding is
            # 1.0 (max cosine similarity) × max recency_weight of any
            # future time, which is <= 1.0. So if our current top-N min
            # is >= 1.0, we can stop. This is conservative but correct.
            if len(scored) >= limit:
                scored.sort(key=lambda s: s.score, reverse=True)
                if scored[limit - 1].score >= 1.0:
                    break

        scored.sort(key=lambda s: s.score, reverse=True)
        return self._hydrate(wax_id, scored, "similar meaning", limit)

    # --- time ------------------------------------------------------------------
    def browse_window(
        self,
        wax_id: WaxId,
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int = 8,
        newest_first: bool = True,
    ) -> tuple[Evidence, ...]:
        entries = self._storage.list_history(
            wax_id, since=since, until=until, limit=limit, newest_first=newest_first
        )
        return tuple(
            history_evidence(e, "history in the requested time range") for e in entries
        )

    def read_around(
        self, wax_id: WaxId, entry_id: UUID, *, before: int = 4, after: int = 2
    ) -> tuple[Evidence, ...]:
        entries = self._storage.list_history_around(
            wax_id, entry_id, before=min(before, 20), after=min(after, 20)
        )
        return tuple(
            history_evidence(e, "history next to the referenced moment")
            for e in entries
        )

    # --- inspection -----------------------------------------------------------------
    def inspect(
        self, wax_id: WaxId, object_id: UUID, object_type: ObjectType | None = None
    ) -> tuple[Evidence, ...]:
        candidates = (
            (object_type,)
            if object_type is not None
            else (
                ObjectType.HISTORY,
                ObjectType.SUMMARY,
                ObjectType.NOTEBOOK,
                ObjectType.KNOWLEDGE_NODE,
                ObjectType.ARTIFACT_VERSION,
                ObjectType.ARTIFACT_CONTENT,
            )
        )
        for candidate in candidates:
            found = hydrate(self._storage, wax_id, candidate, object_id, "inspected")
            if found is not None:
                return (found,)
        return ()

    def trace_sources(
        self, wax_id: WaxId, object_id: UUID, object_type: ObjectType
    ) -> tuple[Evidence, ...]:
        links = self._storage.list_provenance(wax_id, object_type, object_id)
        out: list[Evidence] = []
        for link in links:
            found = hydrate(
                self._storage,
                wax_id,
                link.source_type,
                link.source_id,
                "source of derived item",
            )
            if found is not None:
                out.append(found)
        return tuple(out)

    def check_currency(
        self, wax_id: WaxId, object_id: UUID, object_type: ObjectType
    ) -> tuple[Evidence, ...]:
        """Show an item plus what replaced it (or what it replaced)."""
        found = hydrate(self._storage, wax_id, object_type, object_id, "currency check")
        if found is None:
            return ()
        related: list[Evidence] = [found]
        if object_type is ObjectType.NOTEBOOK:
            entry = self._storage.get_notebook_entry(wax_id, object_id)
            if entry is not None and entry.superseded_by_id is not None:
                newer = hydrate(
                    self._storage,
                    wax_id,
                    ObjectType.NOTEBOOK,
                    entry.superseded_by_id,
                    "replaced this item",
                )
                if newer is not None:
                    related.append(newer)
        if object_type is ObjectType.KNOWLEDGE_NODE:
            for edge in self._storage.list_edges(
                wax_id, object_id, direction="both", limit=8
            ):
                if edge.relation == "superseded_by":
                    other = (
                        edge.target_node_id
                        if edge.source_node_id == object_id
                        else edge.source_node_id
                    )
                    linked = hydrate(
                        self._storage,
                        wax_id,
                        ObjectType.KNOWLEDGE_NODE,
                        other,
                        "supersession link",
                    )
                    if linked is not None:
                        related.append(linked)
        return tuple(related)

    # --- relationships -----------------------------------------------------------------
    def follow_relationships(
        self, wax_id: WaxId, node_id: UUID, *, direction: str = "both", limit: int = 8
    ) -> tuple[Evidence, ...]:
        edges = self._storage.list_edges(
            wax_id, node_id, direction=direction, limit=limit
        )
        return tuple(
            Evidence(
                id=edge.id,
                wax_id=wax_id,
                evidence_type=EvidenceType.KNOWLEDGE_EDGE,
                source_id=edge.id,
                content=clip(
                    f"{edge.source_node_id} -[{edge.relation}]-> {edge.target_node_id}"
                ),
                created_at=edge.created_at,
                relevance_reason="relationship traversal",
                decision=ContextDecision.NEEDS_CHECKING,
                provenance=("relationship derived from history",),
            )
            for edge in edges
        )

    # --- internals ----------------------------------------------------------------------
    def _hydrate(
        self, wax_id: WaxId, scored: list[Scored], reason: str, limit: int
    ) -> tuple[Evidence, ...]:
        # ``scored`` is already sorted by score DESC (rank-then-limit).
        out: list[Evidence] = []
        for item in scored:
            found = hydrate(
                self._storage,
                wax_id,
                item.object_type,
                item.object_id,
                f"{reason} score={item.score:.3f}",
            )
            if found is not None:
                out.append(found)
            if len(out) >= limit:
                break
        return tuple(out)


__all__ = [
    "ALL_INDEXED",
    "DEFAULT_EMBEDDING_PAGE_SIZE",
    "DEFAULT_TOKEN_PAGE_SIZE",
    "Retriever",
]
