"""Database-level Row-Level Security isolation tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Conversation,
    Message,
    MessageContentType,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.migrations import apply_migrations
from waxprep.storage.postgres import PostgresStorage
from waxprep.storage.session import clear_current_wax_id, set_current_wax_id

DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.postgres


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")

    c: psycopg.Connection[Any] = psycopg.connect(DATABASE_URL, autocommit=False)
    with c.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS events, notebook_entries, notebooks, "
            "attachments, messages, conversations, channel_identities, "
            "students, schema_migrations CASCADE"
        )
        cur.execute("DROP FUNCTION IF EXISTS waxprep_lookup_channel(TEXT, TEXT)")
        cur.execute("DROP FUNCTION IF EXISTS waxprep_current_wax_id()")
    c.commit()
    apply_migrations(c)
    yield c
    c.close()


def test_rls_blocks_cross_student_select(conn: psycopg.Connection[Any]) -> None:
    """Without the correct app.current_wax_id, another student's rows are invisible."""
    store = PostgresStorage(conn)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    a = Student(
        wax_id=new_wax_id(),
        created_at=now,
        updated_at=now,
        status=StudentStatus.ACTIVE,
    )
    b = Student(
        wax_id=new_wax_id(),
        created_at=now,
        updated_at=now,
        status=StudentStatus.ACTIVE,
    )
    store.create_student(a)
    store.create_student(b)
    conv = Conversation(id=new_id(), wax_id=a.wax_id, created_at=now, updated_at=now)
    store.create_conversation(conv)
    msg = Message(
        id=new_id(),
        wax_id=a.wax_id,
        conversation_id=conv.id,
        role="student",
        content_type=MessageContentType.TEXT,
        text_body="secret-a",
        created_at=now,
    )
    store.create_message(msg)

    set_current_wax_id(conn, b.wax_id)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM conversations WHERE id = %s", (conv.id,))
        assert cur.fetchone() is None
        cur.execute("SELECT id FROM messages WHERE id = %s", (msg.id,))
        assert cur.fetchone() is None
        cur.execute("SELECT wax_id FROM students WHERE wax_id = %s", (a.wax_id,))
        assert cur.fetchone() is None

    set_current_wax_id(conn, a.wax_id)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM conversations WHERE id = %s", (conv.id,))
        assert cur.fetchone() is not None
        cur.execute("SELECT text_body FROM messages WHERE id = %s", (msg.id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "secret-a"

    clear_current_wax_id(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        count_row = cur.fetchone()
        assert count_row is not None
        assert count_row[0] == 0


def test_rls_rejects_insert_for_other_student(conn: psycopg.Connection[Any]) -> None:
    """WITH CHECK prevents inserting a row under a different WAX ID context."""
    store = PostgresStorage(conn)
    now = datetime(2026, 5, 2, tzinfo=UTC)
    a = Student(
        wax_id=new_wax_id(),
        created_at=now,
        updated_at=now,
        status=StudentStatus.ACTIVE,
    )
    store.create_student(a)
    other = new_wax_id()
    set_current_wax_id(conn, a.wax_id)
    with conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO conversations (id, wax_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (uuid4(), other, now, now),
        )
        conn.commit()
    conn.rollback()
