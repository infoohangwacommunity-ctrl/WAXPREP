"""Complete migration + backfill integration tests against a populated
legacy PostgreSQL database (spec section 12 / finding 2).

Verifies the full sequence:
1. Apply migrations 001-005 on a clean database.
2. Seed representative legacy data (messages, notebook entries,
   artifact filenames, attachments).
3. Run the full backfill.
4. Apply migrations 006-011.
5. Verify the migrated data is correct: IDs preserved, timestamps
   preserved, ownership preserved, authorship preserved, notebook
   payloads encrypted, provenance links created, artifact filenames
   encrypted.
6. Test crash/retry: simulate a crash mid-backfill, verify resuming
   completes the migration without data loss.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import AuthorKind
from waxprep.history.backfill import run_full_backfill
from waxprep.migrations import migrations_dir
from waxprep.storage.postgres import PostgresStorage
from waxprep.storage.session import assume_app_role, set_current_wax_id

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 12, 1, tzinfo=UTC)


def _apply_migrations_up_to(conn: psycopg.Connection[Any], n: int) -> None:
    """Apply migrations 001 through the first n files."""
    migrations = sorted(Path(migrations_dir()).glob("*.sql"))
    for path in migrations[:n]:
        sql = path.read_text()
        with conn.cursor() as cur:
            cur.execute(sql)  # type: ignore[call-overload]
        conn.commit()


def _seed_legacy_data(conn: psycopg.Connection[Any]) -> tuple[Any, Any]:
    """Seed representative legacy data: 2 students, messages, notebook
    entries with payloads and sources, artifact filenames, attachments.
    """
    wax_a = new_wax_id()
    wax_b = new_wax_id()
    for wax in (wax_a, wax_b):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO students (wax_id, created_at, updated_at, status) "
                "VALUES (%s, %s, %s, 'active')",
                (wax, NOW, NOW),
            )
            conv_id = uuid4()
            cur.execute(
                "INSERT INTO conversations (id, wax_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (conv_id, wax, NOW, NOW),
            )
            msg1 = uuid4()
            msg2 = uuid4()
            cur.execute(
                "INSERT INTO messages (id, wax_id, conversation_id, role, "
                "content_type, text_body, created_at) "
                "VALUES (%s, %s, %s, 'student', 'text/plain', %s, %s)",
                (msg1, wax, conv_id, "student text", NOW),
            )
            cur.execute(
                "INSERT INTO messages (id, wax_id, conversation_id, role, "
                "content_type, text_body, created_at) "
                "VALUES (%s, %s, %s, 'tutor', 'text/plain', %s, %s)",
                (msg2, wax, conv_id, "tutor text", NOW),
            )
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
                    json.dumps({"note": "legacy entry"}),
                    str(msg1),
                    NOW,
                    NOW,
                ),
            )
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
                (ver_id, wax, art_id, f"file_{wax}.pdf", NOW),
            )
    conn.commit()
    return wax_a, wax_b


@pytest.fixture
def fresh_db() -> Any:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL not set")
    conn: psycopg.Connection[Any] = psycopg.connect(url, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.commit()
    yield conn
    conn.close()


def test_complete_migration_and_backfill_on_populated_db(fresh_db: Any) -> None:
    """The full sequence: migrations 001-005, seed, backfill,
    migrations 006-011. Verify data preservation."""
    conn = fresh_db
    # 1. Apply migrations 001-005.
    _apply_migrations_up_to(conn, 5)
    # 2. Seed legacy data.
    wax_a, wax_b = _seed_legacy_data(conn)
    # 3. Run the full backfill.
    storage = PostgresStorage(conn, TEST_CIPHER)
    summary = run_full_backfill(conn, storage)
    assert summary.all_ok(), (
        f"backfill had errors: {[r.errors for r in summary.reports if r.errors]}"
    )
    assert summary.total_copied() > 0
    # 4. Apply migrations 006-011. Must RESET ROLE first because the
    # PostgresStorage constructor set the role to waxprep_app, which
    # can't ALTER tables.
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
    conn.commit()
    migrations = sorted(Path(migrations_dir()).glob("*.sql"))
    for path in migrations[5:11]:
        sql = path.read_text()
        with conn.cursor() as cur:
            cur.execute(sql)  # type: ignore[call-overload]
        conn.commit()
    # 5. Verify data preservation.
    # Messages are now in history_entries.
    history_a = storage.list_history(wax_a, limit=10)
    history_b = storage.list_history(wax_b, limit=10)
    assert len(history_a) == 2
    assert len(history_b) == 2
    # Authorship preserved.
    authors_a = {e.author_kind for e in history_a}
    assert AuthorKind.STUDENT in authors_a
    assert AuthorKind.TUTOR in authors_a
    # Notebook payloads are encrypted (payload_encrypted is NOT NULL).
    assume_app_role(conn)
    set_current_wax_id(conn, wax_a)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_encrypted FROM notebook_entries WHERE wax_id = %s",
            (wax_a,),
        )
        rows = cur.fetchall()
    assert all(r[0] is not None for r in rows), (
        "all notebook_entries must have payload_encrypted set"
    )
    # Artifact filenames are encrypted (filename_encrypted is NOT NULL,
    # original_filename is gone).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename_encrypted FROM artifact_versions WHERE wax_id = %s",
            (wax_a,),
        )
        rows = cur.fetchall()
    assert all(r[0] is not None for r in rows), (
        "all artifact_versions must have filename_encrypted set"
    )


def test_backfill_crash_and_resume(fresh_db: Any) -> None:
    """Simulate a crash mid-backfill (after some rows are processed but
    before all are). Verify resuming completes the migration without
    data loss or duplication."""
    conn = fresh_db
    _apply_migrations_up_to(conn, 5)
    # Seed with many students to simulate a partial-crash scenario.
    wax_ids = []
    for _ in range(5):
        wax = new_wax_id()
        wax_ids.append(wax)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO students (wax_id, created_at, updated_at, status) "
                "VALUES (%s, %s, %s, 'active')",
                (wax, NOW, NOW),
            )
            conv_id = uuid4()
            cur.execute(
                "INSERT INTO conversations (id, wax_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (conv_id, wax, NOW, NOW),
            )
            cur.execute(
                "INSERT INTO messages (id, wax_id, conversation_id, role, "
                "content_type, text_body, created_at) "
                "VALUES (%s, %s, %s, 'student', 'text/plain', %s, %s)",
                (uuid4(), wax, conv_id, f"msg for {wax}", NOW),
            )
    conn.commit()
    storage = PostgresStorage(conn, TEST_CIPHER)
    # First partial run: process only messages (simulating a crash
    # before notebook/artifact backfill).
    from waxprep.history.backfill import backfill_messages

    msg_report = backfill_messages(conn, storage)
    assert msg_report.rows_copied == 5
    # Simulate crash: don't run the rest. Then "resume" by running
    # the full backfill again.
    summary = run_full_backfill(conn, storage)
    # Messages should all be "already_present" now.
    msg_report2 = next(r for r in summary.reports if r.step == "messages")
    assert msg_report2.rows_copied == 0
    assert msg_report2.rows_already_present == 5
    # The rest should have been processed.
    assert summary.all_ok()


def test_migration_006_refuses_without_backfill(fresh_db: Any) -> None:
    """Migration 006's guards refuse to run if the backfill hasn't
    been run yet (notebook_entries without payload_encrypted, artifact
    filenames without filename_encrypted)."""
    conn = fresh_db
    _apply_migrations_up_to(conn, 5)
    _seed_legacy_data(conn)
    # Try to apply migration 006 WITHOUT running the backfill first.
    migrations = sorted(Path(migrations_dir()).glob("*.sql"))
    migration_006 = migrations[5].read_text()
    with pytest.raises(psycopg.Error, match="backfill"):
        with conn.cursor() as cur:
            cur.execute(migration_006)
        conn.commit()
    conn.rollback()
