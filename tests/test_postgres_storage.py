"""PostgreSQL storage: encryption at rest, isolation, and the unified flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    NotebookEntry,
    ObjectType,
    ProvenanceLink,
    Student,
    Summary,
    new_id,
)
from waxprep.index.tokens import TokenIndex
from waxprep.storage.postgres import PostgresStorage

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 4, 1, tzinfo=UTC)


def _student(store: PostgresStorage) -> Any:
    wax = new_wax_id()
    store.create_student(Student(wax, NOW, NOW))
    return wax


def _entry(wax: Any, text: str, when: datetime = NOW, **kw: Any) -> HistoryEntry:
    return HistoryEntry(
        new_id(), wax, AuthorKind.STUDENT, "text/plain", text, when, when, **kw
    )


def test_history_text_is_encrypted_in_the_database(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    wax = _student(pg_store)
    entry = pg_store.append_history_entry(
        _entry(wax, "I am always hungry after school")
    )
    with pg_conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT text_encrypted FROM history_entries WHERE id = %s", (entry.id,)
        )
        raw = bytes(cur.fetchone()[0])  # type: ignore[index]
    assert b"hungry" not in raw
    loaded = pg_store.get_history_entry(wax, entry.id)
    assert loaded is not None and loaded.text == "I am always hungry after school"


def test_history_isolation_between_students(pg_store: PostgresStorage) -> None:
    a, b = _student(pg_store), _student(pg_store)
    entry = pg_store.append_history_entry(_entry(a, "only a"))
    assert pg_store.get_history_entry(b, entry.id) is None
    assert pg_store.list_history(b) == ()


def test_duplicate_channel_delivery_is_idempotent(pg_store: PostgresStorage) -> None:
    wax = _student(pg_store)
    first = pg_store.append_history_entry(
        _entry(wax, "hi", channel="wa", channel_ref="m1")
    )
    again = pg_store.append_history_entry(
        _entry(wax, "hi", channel="wa", channel_ref="m1")
    )
    assert again.id == first.id and len(pg_store.list_history(wax)) == 1


def test_read_around_and_time_filters(pg_store: PostgresStorage) -> None:
    wax = _student(pg_store)
    entries = [
        pg_store.append_history_entry(_entry(wax, f"m{i}", NOW + timedelta(minutes=i)))
        for i in range(6)
    ]
    around = pg_store.list_history_around(wax, entries[3].id, before=2, after=1)
    assert [e.text for e in around] == ["m1", "m2", "m3", "m4"]
    recent = pg_store.list_history(wax, since=NOW + timedelta(minutes=4))
    assert [e.text for e in recent] == ["m4", "m5"]


def test_blind_word_index_finds_text_without_storing_words(
    pg_store: PostgresStorage,
) -> None:
    wax = _student(pg_store)
    entry = pg_store.append_history_entry(_entry(wax, "Adeyemi gave worksheet seven"))
    index = TokenIndex(TEST_CIPHER)
    pg_store.put_search_tokens(
        wax, ObjectType.HISTORY, entry.id, NOW, index.hashes(wax, "Adeyemi worksheet")
    )
    hashes = index.hashes(wax, "adeyemi")
    found = pg_store.find_by_tokens(
        wax,
        hashes,
        object_types=(ObjectType.HISTORY,),
        since=None,
        until=None,
        limit=10,
    )
    assert [m.object_id for m in found] == [entry.id]
    stats = pg_store.token_stats(wax, hashes, (ObjectType.HISTORY,))
    assert stats.total_objects == 1 and stats.document_frequency[hashes[0]] == 1


def test_notebook_summary_provenance_round_trip(pg_store: PostgresStorage) -> None:
    wax = _student(pg_store)
    source = pg_store.append_history_entry(_entry(wax, "I study at night"))
    nb = pg_store.get_or_create_notebook(wax, NOW)
    note = pg_store.add_notebook_entry(
        NotebookEntry(new_id(), wax, nb.id, 1, {"o": "studies at night"}, NOW, NOW)
    )
    newer = pg_store.add_notebook_entry(
        NotebookEntry(new_id(), wax, nb.id, 1, {"o": "studies mornings"}, NOW, NOW)
    )
    pg_store.supersede_notebook_entry(wax, note.id, newer.id, NOW)
    assert [e.id for e in pg_store.list_notebook_entries(wax, nb.id)] == [newer.id]
    assert len(pg_store.list_notebook_entries(wax, nb.id, include_superseded=True)) == 2
    summary = pg_store.add_summary(
        Summary(new_id(), wax, "Studied at night.", NOW, NOW, 1, "test", NOW)
    )
    pg_store.add_provenance_links(
        (
            ProvenanceLink(
                new_id(),
                wax,
                ObjectType.SUMMARY,
                summary.id,
                ObjectType.HISTORY,
                source.id,
                NOW,
                AuthorKind.STUDENT,
                "I study at night",
            ),
        )
    )
    link = pg_store.list_provenance(wax, ObjectType.SUMMARY, summary.id)[0]
    assert (
        link.quote == "I study at night"
        and link.source_author_kind is AuthorKind.STUDENT
    )
    assert (
        pg_store.list_unsummarized_history(
            wax, before=NOW + timedelta(days=1), limit=10
        )
        == ()
    )


def test_wrong_student_key_context_cannot_decrypt_rows(
    pg_store: PostgresStorage,
) -> None:
    a, b = _student(pg_store), _student(pg_store)
    entry = pg_store.append_history_entry(_entry(a, "private"))
    assert pg_store.get_history_entry(b, entry.id) is None
