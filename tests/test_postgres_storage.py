"""PostgreSQL storage tests (require DATABASE_URL)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from waxprep.clock import FakeClock
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Conversation,
    Message,
    MessageContentType,
    NotebookEntry,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.migrations import apply_migrations
from waxprep.storage.postgres import PostgresStorage

DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_store() -> Iterator[PostgresStorage]:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")
    import psycopg

    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS events, notebook_entries, notebooks, "
            "attachments, messages, conversations, channel_identities, "
            "students, schema_migrations CASCADE"
        )
    conn.commit()
    apply_migrations(conn)
    yield PostgresStorage(conn)
    conn.close()


def test_postgres_migrations_and_isolation(pg_store: PostgresStorage) -> None:
    clock = FakeClock(datetime(2026, 4, 1, tzinfo=UTC))
    a = Student(
        wax_id=new_wax_id(),
        created_at=clock.now(),
        updated_at=clock.now(),
        status=StudentStatus.ACTIVE,
    )
    b = Student(
        wax_id=new_wax_id(),
        created_at=clock.now(),
        updated_at=clock.now(),
        status=StudentStatus.ACTIVE,
    )
    pg_store.create_student(a)
    pg_store.create_student(b)
    conv = Conversation(
        id=new_id(),
        wax_id=a.wax_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    pg_store.create_conversation(conv)
    msg = Message(
        id=new_id(),
        wax_id=a.wax_id,
        conversation_id=conv.id,
        role="student",
        content_type=MessageContentType.TEXT,
        text_body="only-a",
        created_at=clock.now(),
    )
    pg_store.create_message(msg)
    nb = pg_store.get_or_create_notebook(a.wax_id, clock.now())
    pg_store.add_notebook_entry(
        NotebookEntry(
            id=new_id(),
            wax_id=a.wax_id,
            notebook_id=nb.id,
            schema_version=1,
            payload={"observation": "motorcycle lever insight"},
            source_type="test",
            source_ref=None,
            created_at=clock.now(),
            updated_at=clock.now(),
        )
    )
    assert pg_store.get_conversation(b.wax_id, conv.id) is None
    assert pg_store.get_message(b.wax_id, conv.id, msg.id) is None
    assert pg_store.get_conversation(a.wax_id, conv.id) is not None
    entries = pg_store.list_notebook_entries(a.wax_id, nb.id)
    assert "motorcycle" in entries[0].payload["observation"]
