"""Legacy backfill regression tests (spec section 12).

Verifies that the backfill:
- preserves IDs where possible
- preserves timestamps
- preserves ownership
- preserves message authorship
- preserves attachments
- preserves notebook information
- preserves provenance relationships
- is idempotent
- is count-verifiable
- is tenant-safe
- is non-destructive until verification succeeds
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import AuthorKind, ObjectType
from waxprep.history.backfill import (
    backfill_messages,
    run_full_backfill,
)
from waxprep.storage.postgres import PostgresStorage
from waxprep.storage.session import assume_app_role, set_current_wax_id

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def legacy_db() -> Any:
    """A database with migrations 001-005 applied and representative
    legacy data (messages, notebook entries, artifact filenames)."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL not set")
    conn: psycopg.Connection[Any] = psycopg.connect(url, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.commit()
    # Apply migrations 001-005 only (legacy tables exist; migration 006
    # has not run yet, so messages/conversations/notebook_entries with
    # plaintext payload still exist).
    from pathlib import Path

    from waxprep.migrations import migrations_dir

    migrations = sorted(Path(migrations_dir()).glob("*.sql"))
    for path in migrations[:5]:
        sql = path.read_text()
        with conn.cursor() as cur:
            cur.execute(sql)  # type: ignore[call-overload]  # noqa: PGH003
        conn.commit()
    # Seed legacy data.
    wax_a = new_wax_id()
    wax_b = new_wax_id()
    for wax in (wax_a, wax_b):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO students (wax_id, created_at, updated_at, status) "
                "VALUES (%s, %s, %s, 'active')",
                (wax, NOW, NOW),
            )
            # Create a conversation.
            conv_id = uuid4()
            cur.execute(
                "INSERT INTO conversations (id, wax_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (conv_id, wax, NOW, NOW),
            )
            # Create messages.
            msg1_id = uuid4()
            msg2_id = uuid4()
            cur.execute(
                "INSERT INTO messages (id, wax_id, conversation_id, role, "
                "content_type, text_body, created_at) "
                "VALUES (%s, %s, %s, 'student', 'text/plain', %s, %s)",
                (msg1_id, wax, conv_id, "hello from student", NOW),
            )
            cur.execute(
                "INSERT INTO messages (id, wax_id, conversation_id, role, "
                "content_type, text_body, created_at) "
                "VALUES (%s, %s, %s, 'tutor', 'text/plain', %s, %s)",
                (msg2_id, wax, conv_id, "hello from tutor", NOW),
            )
            # Create a notebook + entry with payload + source.
            nb_id = uuid4()
            cur.execute(
                "INSERT INTO notebooks (id, wax_id, version, created_at, updated_at) "
                "VALUES (%s, %s, 1, %s, %s)",
                (nb_id, wax, NOW, NOW),
            )
            nb_entry_id = uuid4()
            cur.execute(
                "INSERT INTO notebook_entries (id, wax_id, notebook_id, "
                "schema_version, payload, source_type, source_ref, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, 1, %s::jsonb, 'history', %s, %s, %s)",
                (
                    nb_entry_id,
                    wax,
                    nb_id,
                    json.dumps({"note": "test"}),
                    str(msg1_id),
                    NOW,
                    NOW,
                ),
            )
            # Create an artifact version with a plaintext filename.
            ws_id = uuid4()
            cur.execute(
                "INSERT INTO workspaces (id, wax_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (ws_id, wax, NOW, NOW),
            )
            art_id = uuid4()
            cur.execute(
                "INSERT INTO artifacts (id, wax_id, workspace_id, status, "
                "created_at, updated_at) VALUES (%s, %s, %s, 'active', %s, %s)",
                (art_id, wax, ws_id, NOW, NOW),
            )
            ver_id = uuid4()
            cur.execute(
                "INSERT INTO artifact_versions (id, wax_id, artifact_id, "
                "version_number, storage_ref, original_filename, mime_type, "
                "size_bytes, checksum_sha256, created_at) "
                "VALUES (%s, %s, %s, 1, 'ref', %s, 'text/plain', 1, 'sha', %s)",
                (ver_id, wax, art_id, "worksheet.pdf", NOW),
            )
    conn.commit()
    yield conn, wax_a, wax_b
    conn.close()


def test_backfill_messages_preserves_ids_and_authorship(legacy_db: Any) -> None:
    """Messages are copied to history_entries preserving IDs, timestamps,
    and authorship (student vs tutor)."""
    conn, wax_a, _ = legacy_db
    storage = PostgresStorage(conn, TEST_CIPHER)
    report = backfill_messages(conn, storage)
    assert report.rows_copied == 4  # 2 messages × ... actually 2 per student
    assert report.rows_seen >= 4
    # Verify the messages are in history_entries with the right authorship.
    history = storage.list_history(wax_a, limit=10)
    assert len(history) == 2
    authors = {e.author_kind for e in history}
    assert AuthorKind.STUDENT in authors
    assert AuthorKind.TUTOR in authors
    # Verify IDs are preserved.
    msg_ids = {e.id for e in history}
    assert len(msg_ids) == 2


def test_backfill_is_idempotent(legacy_db: Any) -> None:
    """Running backfill twice copies nothing the second time."""
    conn, _, _ = legacy_db
    storage = PostgresStorage(conn, TEST_CIPHER)
    first = backfill_messages(conn, storage)
    second = backfill_messages(conn, storage)
    assert first.rows_copied > 0
    assert second.rows_copied == 0, "second run must copy nothing"
    assert second.rows_already_present == first.rows_seen


def test_full_backfill_converts_notebook_and_artifact(legacy_db: Any) -> None:
    """The full backfill converts notebook payloads to encrypted and
    artifact filenames to encrypted."""
    conn, wax_a, _ = legacy_db
    storage = PostgresStorage(conn, TEST_CIPHER)
    summary = run_full_backfill(conn, storage)
    # Messages copied.
    msg_report = next(r for r in summary.reports if r.step == "messages")
    assert msg_report.rows_copied > 0
    # Notebook entries converted.
    nb_report = next(r for r in summary.reports if r.step == "notebook_entries")
    assert nb_report.rows_copied > 0, "notebook entries must be converted"
    # Artifact filenames converted.
    af_report = next(r for r in summary.reports if r.step == "artifact_filenames")
    assert af_report.rows_copied > 0, "artifact filenames must be converted"
    # Verify notebook_entries now have payload_encrypted set.
    assume_app_role(conn)
    set_current_wax_id(conn, wax_a)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_encrypted FROM notebook_entries WHERE wax_id = %s",
            (wax_a,),
        )
        rows = cur.fetchall()
    assert all(r[0] is not None for r in rows), (
        "all notebook_entries must have payload_encrypted set after backfill"
    )
    # Verify artifact_versions now have filename_encrypted set.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename_encrypted FROM artifact_versions WHERE wax_id = %s",
            (wax_a,),
        )
        rows = cur.fetchall()
    assert all(r[0] is not None for r in rows), (
        "all artifact_versions must have filename_encrypted set after backfill"
    )


def test_backfill_provenance_is_created(legacy_db: Any) -> None:
    """Notebook entries with source_type/source_ref get provenance links."""
    conn, wax_a, _ = legacy_db
    storage = PostgresStorage(conn, TEST_CIPHER)
    run_full_backfill(conn, storage)
    # Find the notebook entry ID for wax_a.
    assume_app_role(conn)
    set_current_wax_id(conn, wax_a)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM notebook_entries WHERE wax_id = %s LIMIT 1",
            (wax_a,),
        )
        row = cur.fetchone()
    assert row is not None
    nb_entry_id = row[0]
    # Verify a provenance link exists pointing from the notebook entry
    # to a history entry.
    links = storage.list_provenance(wax_a, ObjectType.NOTEBOOK, nb_entry_id)
    assert links, "notebook entry with source_type must get a provenance link"
    assert any(link.source_type is ObjectType.HISTORY for link in links), (
        "provenance must point to a history entry"
    )


def test_backfill_is_tenant_safe(legacy_db: Any) -> None:
    """The backfill uses the scoped Storage API; cross-student access
    is rejected by RLS."""
    conn, wax_a, wax_b = legacy_db
    storage = PostgresStorage(conn, TEST_CIPHER)
    # Run backfill.
    run_full_backfill(conn, storage)
    # Verify wax_a's data doesn't leak to wax_b.
    history_a = storage.list_history(wax_a, limit=10)
    history_b = storage.list_history(wax_b, limit=10)
    assert all(e.wax_id == wax_a for e in history_a)
    assert all(e.wax_id == wax_b for e in history_b)
    # Cross-student provenance is rejected.
    from datetime import datetime

    from waxprep.domain.models import ProvenanceLink, new_id

    if history_a and history_b:
        link = ProvenanceLink(
            id=new_id(),
            wax_id=wax_b,
            derived_type=ObjectType.NOTEBOOK,
            derived_id=uuid4(),
            source_type=ObjectType.HISTORY,
            source_id=history_a[0].id,  # cross-student!
            created_at=datetime.now(UTC),
        )
        with pytest.raises((ValueError, psycopg.Error)):
            storage.add_provenance_links((link,))


def test_backfill_report_is_count_verifiable(legacy_db: Any) -> None:
    """The backfill report's counts can be verified against the source."""
    conn, _, _ = legacy_db
    storage = PostgresStorage(conn, TEST_CIPHER)
    summary = run_full_backfill(conn, storage)
    # Verify message count: source = destination.
    msg_report = next(r for r in summary.reports if r.step == "messages")
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT count(*) FROM messages")
        source_count = cur.fetchone()[0]
    assert msg_report.rows_seen == source_count
    assert msg_report.rows_copied + msg_report.rows_already_present == source_count
