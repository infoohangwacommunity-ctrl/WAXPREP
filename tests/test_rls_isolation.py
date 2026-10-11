"""Database-level Row-Level Security isolation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import AuthorKind, HistoryEntry, Student, new_id
from waxprep.storage.postgres import PostgresStorage
from waxprep.storage.session import (
    assume_app_role,
    clear_current_wax_id,
    set_current_wax_id,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 5, 1, tzinfo=UTC)


def test_rls_blocks_cross_student_select(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    a, b = new_wax_id(), new_wax_id()
    pg_store.create_student(Student(a, NOW, NOW))
    pg_store.create_student(Student(b, NOW, NOW))
    entry = pg_store.append_history_entry(
        HistoryEntry(
            new_id(), a, AuthorKind.STUDENT, "text/plain", "secret-a", NOW, NOW
        )
    )
    assume_app_role(pg_conn)
    set_current_wax_id(pg_conn, b)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM history_entries WHERE id = %s", (entry.id,))
        assert cur.fetchone() is None
    clear_current_wax_id(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM history_entries")
        row = cur.fetchone()
        assert row is not None and row[0] == 0


def test_rls_rejects_insert_for_another_student(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    a = new_wax_id()
    pg_store.create_student(Student(a, NOW, NOW))
    assume_app_role(pg_conn)
    set_current_wax_id(pg_conn, a)
    with pg_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO history_entries (id, wax_id, author_kind, content_kind, "
            "occurred_at, recorded_at) VALUES (%s, %s, 'student', 'text/plain', %s, %s)",
            (uuid4(), new_wax_id(), NOW, NOW),
        )
        pg_conn.commit()
    pg_conn.rollback()
