"""CI history retrieval — pagination, keyword, time-range, RLS isolation,
and the pgvector Python-side fallback.

The first block of tests runs against ``InMemoryStorage`` (no DB
required) so the quality gate exercises the new storage signatures
in every commit. The second block is marked ``postgres`` and runs
only when ``DATABASE_URL`` is set — it exercises the real RLS
policies and the migration 016 indexes against a live Postgres.

The brief: ``Postgres-marked tests will error in sandbox without a
real DB; document this.`` — see the report at
``/home/z/my-project/ecc-org/coordination/findings/impl-database.md``
for the sandbox outcome.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from support import TEST_CIPHER  # noqa: F401  — re-exported for conftest
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    EmbeddingRecord,
    HistoryEntry,
    ObjectType,
    Student,
    new_id,
)
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.storage.memory import InMemoryStorage
from waxprep.storage.protocol import HistoryCursor, Storage

NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _say(
    storage: Storage,
    wax_id: Any,
    text: str,
    when: datetime = NOW,
    *,
    author: AuthorKind = AuthorKind.STUDENT,
    thread_ref: str | None = None,
    channel_ref: str | None = None,
) -> HistoryEntry:
    """Insert one student-authored history entry at ``when``."""
    return storage.append_history_entry(
        HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=author,
            content_kind="text/plain",
            text=text,
            occurred_at=when,
            recorded_at=when,
            channel="test" if channel_ref else None,
            channel_ref=channel_ref,
            thread_ref=thread_ref,
        )
    )


def _seed_stream(storage: Storage, wax_id: Any, n: int) -> list[HistoryEntry]:
    """Insert ``n`` history entries one minute apart starting at NOW."""
    return [
        _say(
            storage,
            wax_id,
            f"message number {i:03d}",
            when=NOW + timedelta(minutes=i),
        )
        for i in range(n)
    ]


def _embed(provider: DeterministicEmbeddingProvider, text: str) -> tuple[float, ...]:
    """Sync wrapper around the async ``embed`` so sync tests can build
    vectors without an event loop."""
    return asyncio.run(provider.embed(text))


# ===========================================================================
# In-memory tests — always run in the sandbox quality gate.
# ===========================================================================
class TestListHistoryPaginatedInMemory:
    """Cursor pagination walks the full history stream."""

    def test_pagination_walks_full_history_in_memory(self) -> None:
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        seeded = _seed_stream(store, wax, 60)  # 60 entries, page_size=25

        collected: list[HistoryEntry] = []
        cursor: HistoryCursor = None
        pages = 0
        while True:
            page, cursor = store.list_history_paginated(
                wax, before=cursor, page_size=25, newest_first=True
            )
            pages += 1
            collected.extend(page)
            if cursor is None:
                break
            if pages > 10:  # safety belt
                break

        # Newest-first: the most recent message is first.
        assert pages == 3  # 25 + 25 + 10
        assert len(collected) == 60
        expected_ids = [e.id for e in reversed(seeded)]
        assert [e.id for e in collected] == expected_ids
        # Cursor is None at end of stream.
        assert cursor is None

    def test_pagination_ascending_order_in_memory(self) -> None:
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        seeded = _seed_stream(store, wax, 5)
        collected: list[HistoryEntry] = []
        cursor: HistoryCursor = None
        while True:
            page, cursor = store.list_history_paginated(
                wax, before=cursor, page_size=2, newest_first=False
            )
            collected.extend(page)
            if cursor is None:
                break
        assert [e.id for e in collected] == [e.id for e in seeded]


class TestKeywordSearchInMemory:
    """Substring search returns matches across the whole stream."""

    def test_keyword_search_returns_matches_in_memory(self) -> None:
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        _say(store, wax, "Tell me about photosynthesis", NOW)
        _say(store, wax, "my favourite football club", NOW + timedelta(minutes=1))
        _say(store, wax, "Photosynthesis experiment results", NOW + timedelta(minutes=2))
        _say(store, wax, "unrelated weather chat", NOW + timedelta(minutes=3))

        hits = store.search_history_by_keyword(wax, "photo", limit=10)
        assert len(hits) == 2
        # Newest-first ordering.
        assert hits[0].text is not None and "experiment results" in hits[0].text
        assert hits[1].text is not None and "Tell me about" in hits[1].text

    def test_keyword_search_is_case_insensitive_in_memory(self) -> None:
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        _say(store, wax, "WAEC preparation plan", NOW)
        hits = store.search_history_by_keyword(wax, "waec", limit=10)
        assert len(hits) == 1
        assert hits[0].text == "WAEC preparation plan"

    def test_keyword_search_respects_limit_in_memory(self) -> None:
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        for i in range(10):
            _say(store, wax, f"alpha entry {i}", NOW + timedelta(minutes=i))
        hits = store.search_history_by_keyword(wax, "alpha", limit=3)
        assert len(hits) == 3
        # Newest first.
        assert hits[0].text is not None and "alpha entry 9" in hits[0].text


class TestTimeRangeSearchInMemory:
    def test_time_range_search_in_memory(self) -> None:
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        _say(store, wax, "morning note", NOW)
        _say(store, wax, "afternoon note", NOW + timedelta(hours=6))
        _say(store, wax, "evening note", NOW + timedelta(hours=18))
        start = NOW + timedelta(hours=5)
        end = NOW + timedelta(hours=20)
        hits = store.search_history_by_time_range(
            wax, start_ts=start, end_ts=end, limit=200
        )
        assert {h.text for h in hits} == {"afternoon note", "evening note"}

    def test_time_range_search_half_open_in_memory(self) -> None:
        """end_ts is exclusive — a row whose occurred_at == end_ts is
        not returned."""
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        _say(store, wax, "boundary", NOW + timedelta(hours=1))
        hits = store.search_history_by_time_range(
            wax,
            start_ts=NOW,
            end_ts=NOW + timedelta(hours=1),
            limit=200,
        )
        assert hits == ()


class TestPgvectorFallbackInMemory:
    """When ``has_pgvector()`` is False, semantic search still works via
    the Python-side cosine fallback over decrypted embeddings."""

    def test_has_pgvector_is_false_for_in_memory(self) -> None:
        store = InMemoryStorage()
        assert store.has_pgvector() is False

    def test_find_by_meaning_python_fallback_in_memory(self) -> None:
        store = InMemoryStorage()
        wax = new_wax_id()
        store.create_student(Student(wax, NOW, NOW))
        embedder = DeterministicEmbeddingProvider(dimensions=16)

        # Two history entries; the first semantically matches the
        # query (identical text).
        e_match = _say(store, wax, "completing the square", NOW)
        e_other = _say(store, wax, "my favourite football club", NOW + timedelta(minutes=1))

        query_vec = _embed(embedder, "completing the square")
        store.put_embedding(
            EmbeddingRecord(
                id=new_id(),
                wax_id=wax,
                object_type=ObjectType.HISTORY,
                object_id=e_match.id,
                object_time=e_match.occurred_at,
                model="mock-16",
                vector=_embed(embedder, e_match.text or ""),
                created_at=NOW,
            )
        )
        store.put_embedding(
            EmbeddingRecord(
                id=new_id(),
                wax_id=wax,
                object_type=ObjectType.HISTORY,
                object_id=e_other.id,
                object_time=e_other.occurred_at,
                model="mock-16",
                vector=_embed(embedder, e_other.text or ""),
                created_at=NOW,
            )
        )

        hits = store.find_by_meaning(
            wax,
            query_vec,
            object_types=(ObjectType.HISTORY,),
            since=None,
            until=None,
            limit=2,
        )
        assert hits
        assert hits[0].object_id == e_match.id
        # Score is cosine similarity in (-1, 1].
        assert hits[0].score > 0.99  # identical query vector
        # The fallback path was actually exercised.
        assert store.has_pgvector() is False


# ===========================================================================
# Postgres tests — marked ``postgres`` and skipped without DATABASE_URL.
# They exercise real RLS + the migration 016 indexes + the optional
# pgvector path (if the extension is installed on the test DB).
# ===========================================================================


@pytest.mark.postgres
class TestListHistoryPaginatedPostgres:
    def test_pagination_walks_full_history_postgres(
        self, pg_store: Any, pg_conn: Any
    ) -> None:
        wax = new_wax_id()
        pg_store.create_student(Student(wax, NOW, NOW))
        seeded = _seed_stream(pg_store, wax, 60)
        collected: list[HistoryEntry] = []
        cursor: HistoryCursor = None
        pages = 0
        while True:
            page, cursor = pg_store.list_history_paginated(
                wax, before=cursor, page_size=25, newest_first=True
            )
            pages += 1
            collected.extend(page)
            if cursor is None:
                break
            if pages > 10:
                break
        assert pages == 3
        assert len(collected) == 60
        assert [e.id for e in collected] == [e.id for e in reversed(seeded)]


@pytest.mark.postgres
class TestKeywordSearchPostgres:
    def test_keyword_search_returns_matches_postgres(self, pg_store: Any) -> None:
        wax = new_wax_id()
        pg_store.create_student(Student(wax, NOW, NOW))
        _say(pg_store, wax, "Tell me about photosynthesis", NOW)
        _say(pg_store, wax, "unrelated weather chat", NOW + timedelta(minutes=1))
        _say(pg_store, wax, "Photosynthesis experiment results", NOW + timedelta(minutes=2))
        hits = pg_store.search_history_by_keyword(wax, "photo", limit=10)
        assert len(hits) == 2
        assert "experiment results" in hits[0].text


@pytest.mark.postgres
class TestTimeRangeSearchPostgres:
    def test_time_range_search_postgres(self, pg_store: Any) -> None:
        wax = new_wax_id()
        pg_store.create_student(Student(wax, NOW, NOW))
        _say(pg_store, wax, "morning note", NOW)
        _say(pg_store, wax, "afternoon note", NOW + timedelta(hours=6))
        _say(pg_store, wax, "evening note", NOW + timedelta(hours=18))
        hits = pg_store.search_history_by_time_range(
            wax,
            start_ts=NOW + timedelta(hours=5),
            end_ts=NOW + timedelta(hours=20),
            limit=200,
        )
        assert {h.text for h in hits} == {"afternoon note", "evening note"}


@pytest.mark.postgres
class TestRlsIsolationPostgres:
    """Real RLS isolation — student A cannot see student B's history.

    The brief's task 7 requires this proof against the actual RLS
    policies from migration 002/005.
    """

    def test_pagination_does_not_leak_cross_student_postgres(
        self, pg_store: Any
    ) -> None:
        a, b = new_wax_id(), new_wax_id()
        pg_store.create_student(Student(a, NOW, NOW))
        pg_store.create_student(Student(b, NOW, NOW))
        _seed_stream(pg_store, a, 30)
        # Student B has no history.
        collected: list[HistoryEntry] = []
        cursor: HistoryCursor = None
        while True:
            page, cursor = pg_store.list_history_paginated(
                b, before=cursor, page_size=25, newest_first=True
            )
            collected.extend(page)
            if cursor is None:
                break
        assert collected == []

    def test_keyword_search_does_not_leak_cross_student_postgres(
        self, pg_store: Any
    ) -> None:
        a, b = new_wax_id(), new_wax_id()
        pg_store.create_student(Student(a, NOW, NOW))
        pg_store.create_student(Student(b, NOW, NOW))
        _say(pg_store, a, "alpha secret phrase only A should see", NOW)
        hits = pg_store.search_history_by_keyword(b, "alpha", limit=10)
        assert hits == ()

    def test_time_range_search_does_not_leak_cross_student_postgres(
        self, pg_store: Any
    ) -> None:
        a, b = new_wax_id(), new_wax_id()
        pg_store.create_student(Student(a, NOW, NOW))
        pg_store.create_student(Student(b, NOW, NOW))
        _say(pg_store, a, "alpha secret in time window", NOW)
        hits = pg_store.search_history_by_time_range(
            b,
            start_ts=NOW - timedelta(hours=1),
            end_ts=NOW + timedelta(hours=1),
            limit=200,
        )
        assert hits == ()


@pytest.mark.postgres
class TestPgvectorOptionalPostgres:
    """When pgvector is installed, find_by_meaning uses the SQL path.
    When it isn't (Render free tier), the Python fallback runs.

    This test exercises both paths via ``has_pgvector()`` and verifies
    the same ranking either way. If pgvector is installed on the test
    DB, both branches are exercised; if not, only the fallback is.
    """

    def test_has_pgvector_returns_bool(self, pg_store: Any) -> None:
        # The check must not raise — it must return a bool.
        assert isinstance(pg_store.has_pgvector(), bool)

    def test_find_by_meaning_works_with_or_without_pgvector(
        self, pg_store: Any
    ) -> None:
        wax = new_wax_id()
        pg_store.create_student(Student(wax, NOW, NOW))
        embedder = DeterministicEmbeddingProvider(dimensions=16)
        e_match = _say(pg_store, wax, "completing the square", NOW)
        e_other = _say(pg_store, wax, "my favourite football club", NOW + timedelta(minutes=1))
        for entry in (e_match, e_other):
            pg_store.put_embedding(
                EmbeddingRecord(
                    id=new_id(),
                    wax_id=wax,
                    object_type=ObjectType.HISTORY,
                    object_id=entry.id,
                    object_time=entry.occurred_at,
                    model="mock-16",
                    vector=_embed(embedder, entry.text or ""),
                    created_at=NOW,
                )
            )
        # If pgvector is installed, the put_embedding call above wrote
        # the plaintext vector_plain column; find_by_meaning uses the
        # SQL path. If not, it falls back to Python-side cosine. Both
        # paths return the same ranking for identical vectors.
        query_vec = _embed(embedder, "completing the square")
        hits = pg_store.find_by_meaning(
            wax,
            query_vec,
            object_types=(ObjectType.HISTORY,),
            since=None,
            until=None,
            limit=2,
        )
        assert hits
        assert hits[0].object_id == e_match.id
        assert hits[0].score > 0.99

    def test_find_by_meaning_python_fallback_can_be_forced(
        self, pg_store: Any
    ) -> None:
        """Force the Python fallback by clearing the pgvector cache and
        pretending the extension is missing. Verifies the fallback path
        is correct regardless of the test DB's pgvector state.
        """
        wax = new_wax_id()
        pg_store.create_student(Student(wax, NOW, NOW))
        embedder = DeterministicEmbeddingProvider(dimensions=16)
        e_match = _say(pg_store, wax, "completing the square", NOW)
        e_other = _say(pg_store, wax, "my favourite football club", NOW + timedelta(minutes=1))
        for entry in (e_match, e_other):
            pg_store.put_embedding(
                EmbeddingRecord(
                    id=new_id(),
                    wax_id=wax,
                    object_type=ObjectType.HISTORY,
                    object_id=entry.id,
                    object_time=entry.occurred_at,
                    model="mock-16",
                    vector=_embed(embedder, entry.text or ""),
                    created_at=NOW,
                )
            )
        # Force the Python fallback by overriding the cache.
        pg_store._pgvector_available = False  # type: ignore[attr-defined]
        query_vec = _embed(embedder, "completing the square")
        hits = pg_store.find_by_meaning(
            wax,
            query_vec,
            object_types=(ObjectType.HISTORY,),
            since=None,
            until=None,
            limit=2,
        )
        assert hits
        assert hits[0].object_id == e_match.id
        assert hits[0].score > 0.99
        # Reset cache so other tests get the real value.
        pg_store._reset_pgvector_cache()  # type: ignore[attr-defined]

