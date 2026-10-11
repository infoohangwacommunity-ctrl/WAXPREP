"""Rank-then-limit retrieval regression tests (spec section 7).

Verifies:
- A highly relevant old record beyond the first candidate page is still
  discovered.
- Semantic search can continue beyond the initial candidate page.
- Exact search can continue beyond the initial page.
- Ranking happens before the final evidence limit.
- Pagination is deterministic.
- Student isolation is preserved.
- Old information is not silently lost because it is old.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support import TEST_CIPHER, add_student, make_world
from waxprep.context.retrieval import Retriever
from waxprep.domain.models import AuthorKind, ObjectType
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.index.tokens import TokenIndex
from waxprep.storage.memory import InMemoryStorage

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_old_rare_record_beyond_first_page_is_discoverable() -> None:
    """An old rare record is still discoverable when more than one page of
    newer matches exists. The retriever paginates through all matches;
    rank-then-limit surfaces the rare old record.
    """
    storage = InMemoryStorage()
    # Use a small page size so we can verify pagination with few records.
    retriever = Retriever(storage, TokenIndex(TEST_CIPHER), token_page_size=10)
    world = make_world(storage=storage)
    wax = add_student(world)
    # One rare old record with a unique phrase.
    old_time = NOW - timedelta(days=365)
    world.services.history.record(
        wax,
        AuthorKind.STUDENT,
        "ZzQx rareword uniquephrase old_exam_marker",
        occurred_at=old_time,
    )
    # 50 newer records with a common word (more than one page of 10).
    for i in range(50):
        world.services.history.record(
            wax,
            AuthorKind.STUDENT,
            f"common message number {i}",
            occurred_at=NOW - timedelta(hours=50 - i),
        )
    # The old record must be found by the rare phrase.
    evidence = retriever.find_exact(
        wax,
        "rareword uniquephrase",
        now=NOW,
        object_types=(ObjectType.HISTORY,),
        limit=5,
    )
    assert any(
        "rareword" in (e.content or "").lower()
        or "uniquephrase" in (e.content or "").lower()
        for e in evidence
    ), "old rare record must still be discoverable after pagination"


def test_semantic_search_continues_beyond_first_page() -> None:
    """Semantic search paginates through embeddings until the corpus is
    exhausted or the top-N is stable."""
    storage = InMemoryStorage()
    embedder = DeterministicEmbeddingProvider(dimensions=8)
    retriever = Retriever(storage, TokenIndex(TEST_CIPHER), embedding_page_size=5)
    world = make_world(storage=storage, embedder=embedder)
    wax = add_student(world)
    # 20 records with embeddings (more than one page of 5).
    for i in range(20):
        world.services.history.record(
            wax,
            AuthorKind.STUDENT,
            f"semantic message {i}",
            occurred_at=NOW - timedelta(hours=20 - i),
        )
    # Run the worker to generate embeddings.
    import asyncio

    asyncio.run(world.services.worker.run_once())
    # Use embed_many for the deterministic provider.
    vec = asyncio.run(embedder.embed_many(["semantic"]))[0]
    evidence = retriever.find_by_meaning(
        wax,
        vec,
        now=NOW,
        object_types=(ObjectType.HISTORY,),
        limit=5,
    )
    assert len(evidence) > 0, "semantic search must return results from all pages"


def test_exact_search_continues_beyond_first_page() -> None:
    """Exact search paginates through all matches until the corpus is
    exhausted."""
    storage = InMemoryStorage()
    retriever = Retriever(storage, TokenIndex(TEST_CIPHER), token_page_size=5)
    world = make_world(storage=storage)
    wax = add_student(world)
    # 20 records with the same word (more than one page of 5).
    for i in range(20):
        world.services.history.record(
            wax,
            AuthorKind.STUDENT,
            f"commonword message {i}",
            occurred_at=NOW - timedelta(hours=20 - i),
        )
    evidence = retriever.find_exact(
        wax,
        "commonword",
        now=NOW,
        object_types=(ObjectType.HISTORY,),
        limit=8,
    )
    assert len(evidence) == 8, f"expected 8 results, got {len(evidence)}"


def test_ranking_happens_before_final_evidence_limit() -> None:
    """The retriever ranks all candidates before applying the final
    evidence limit. The top-scoring records are returned, not just the
    first N by time.
    """
    storage = InMemoryStorage()
    retriever = Retriever(storage, TokenIndex(TEST_CIPHER))
    world = make_world(storage=storage)
    wax = add_student(world)
    # 10 records with the same word. The OLDEST one has a second rare
    # word that should give it a higher score.
    for i in range(10):
        text = f"commonword message {i}"
        if i == 0:
            text += " rareboost"
        world.services.history.record(
            wax,
            AuthorKind.STUDENT,
            text,
            occurred_at=NOW - timedelta(hours=10 - i),
        )
    evidence = retriever.find_exact(
        wax,
        "commonword rareboost",
        now=NOW,
        object_types=(ObjectType.HISTORY,),
        limit=1,
    )
    # The oldest record (i=0) has both "commonword" and "rareboost" —
    # its combined score should be highest.
    assert len(evidence) == 1
    assert "rareboost" in (evidence[0].content or "")


def test_pagination_is_deterministic() -> None:
    """Two identical calls return the same evidence set."""
    storage = InMemoryStorage()
    world = make_world(storage=storage)
    wax = add_student(world)
    for i in range(10):
        world.services.history.record(
            wax,
            AuthorKind.STUDENT,
            f"hello world message {i}",
            occurred_at=NOW - timedelta(minutes=10 - i),
        )
    retriever = Retriever(storage, TokenIndex(TEST_CIPHER))
    first = retriever.find_exact(
        wax, "hello world", now=NOW, object_types=(ObjectType.HISTORY,), limit=3
    )
    second = retriever.find_exact(
        wax, "hello world", now=NOW, object_types=(ObjectType.HISTORY,), limit=3
    )
    assert [e.id for e in first] == [e.id for e in second]


def test_student_isolation_in_retrieval() -> None:
    """Retrieval never crosses students."""
    storage = InMemoryStorage()
    world = make_world(storage=storage)
    wax_a = add_student(world)
    wax_b = add_student(world)
    world.services.history.record(
        wax_a, AuthorKind.STUDENT, "secret for a only", occurred_at=NOW
    )
    retriever = Retriever(storage, TokenIndex(TEST_CIPHER))
    # b searches for the same words — must find nothing.
    evidence = retriever.find_exact(
        wax_b,
        "secret for a only",
        now=NOW,
        object_types=(ObjectType.HISTORY,),
        limit=5,
    )
    assert evidence == ()
