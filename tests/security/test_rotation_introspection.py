"""Rotation introspection tests — the structural fix for the
``security/rotation.py:208-224`` hardcoded-column-list issue.

The Founder's brief: *"Replace the hardcoded list with schema
introspection: query ``information_schema.columns`` for all columns
ending in ``_encrypted``, and use that as the rotation inventory.
This is a structural fix, not a prompt fix. It catches future
columns automatically."*

These tests verify:

1. ``discover_encrypted_columns`` parses the ``purpose=...`` column
   comment set by migrations 018 + 021 + 022 correctly.

2. The new columns introduced by this work —
   ``learning_states.payload_encrypted`` and
   ``student_calendar.notes_encrypted`` — are picked up automatically
   (no Python-side hardcoded list update needed).

3. The fail-closed contract is preserved: a column with no
   ``purpose=...`` comment raises ``ValueError`` listing the missing
   registration, and rotation refuses to run.

4. (Postgres-marked) against a real DB: every ``*_encrypted`` column
   is discovered and matches the cipher purpose the write path
   actually uses — verifying that migration 022's column comments
   match the storage layer's hardcoded purposes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from waxprep.security.rotation import (
    RotationTarget,
    discover_encrypted_columns,
)

# Expected purpose strings, mirroring the storage layer's
# ``_enc(wax_id, purpose, ...)`` call sites. Migration 022 + the
# column comments set by migrations 018 and 021 must agree with this
# set. The list is intentionally exhaustive — adding a new
# ``*_encrypted`` column requires updating BOTH the storage write
# path AND the matching column comment, otherwise this test fails.
EXPECTED_PURPOSES: dict[tuple[str, str], str] = {
    ("artifact_contents", "text_encrypted"): "artifact_content.text",
    ("artifact_versions", "filename_encrypted"): "artifact.filename",
    ("context_investigations", "input_encrypted"): "investigation.input",
    ("context_investigations", "result_encrypted"): "investigation.result",
    ("history_entries", "text_encrypted"): "history.text",
    # New column from migration 018 — the structural fix's primary
    # proof: this entry was previously absent from the hardcoded list
    # and would have been silently orphaned during key rotation.
    ("learning_states", "payload_encrypted"): "learning_state.payload",
    ("knowledge_edges", "properties_encrypted"): "knowledge.edge_properties",
    ("knowledge_edges", "relation_encrypted"): "knowledge.relation",
    ("knowledge_nodes", "description_encrypted"): "knowledge.description",
    ("knowledge_nodes", "label_encrypted"): "knowledge.label",
    ("knowledge_nodes", "node_type_encrypted"): "knowledge.node_type",
    ("knowledge_nodes", "properties_encrypted"): "knowledge.properties",
    ("notebook_entries", "payload_encrypted"): "notebook.payload",
    ("provenance_links", "quote_encrypted"): "provenance.quote",
    # New column from migration 021 — same proof as above.
    ("student_calendar", "notes_encrypted"): "student_calendar.notes",
    ("summaries", "text_encrypted"): "summary.text",
    ("search_embeddings", "vector_encrypted"): "embedding.vector",
}


def _make_fake_conn(rows: list[dict[str, Any]]) -> Any:
    """Build a fake psycopg connection whose cursor returns the given
    rows for any query. The cursor is a context manager that yields
    itself; ``fetchall`` returns the canned rows."""

    class _FakeCursor:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple[Any, ...]]] = []

        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(
            self, sql: str, params: tuple[Any, ...] | None = None
        ) -> None:
            self.executed.append((sql, params or ()))

        def fetchall(self) -> list[dict[str, Any]]:
            return list(rows)

    class _FakeConn:
        def __init__(self) -> None:
            self.cursor_obj = _FakeCursor()

        def cursor(self, row_factory: Any = None) -> _FakeCursor:
            return self.cursor_obj

    return _FakeConn()


def test_discover_picks_up_new_learning_states_column() -> None:
    """The structural fix's primary proof: ``discover_encrypted_columns``
    finds the new ``learning_states.payload_encrypted`` column when its
    column comment is set. Previously this column would have been
    silently orphaned during key rotation (the bug the Founder's
    brief called out at ``security/rotation.py:208-224``)."""
    rows = [
        {
            "table_name": "learning_states",
            "column_name": "payload_encrypted",
            "comment": "purpose=learning_state.payload",
        }
    ]
    conn = _make_fake_conn(rows)
    targets = discover_encrypted_columns(conn)
    assert RotationTarget(
        table="learning_states",
        column="payload_encrypted",
        purpose="learning_state.payload",
    ) in targets


def test_discover_picks_up_new_student_calendar_column() -> None:
    """Same proof for ``student_calendar.notes_encrypted`` (migration 021)."""
    rows = [
        {
            "table_name": "student_calendar",
            "column_name": "notes_encrypted",
            "comment": "purpose=student_calendar.notes",
        }
    ]
    conn = _make_fake_conn(rows)
    targets = discover_encrypted_columns(conn)
    assert RotationTarget(
        table="student_calendar",
        column="notes_encrypted",
        purpose="student_calendar.notes",
    ) in targets


def test_discover_picks_up_every_expected_column() -> None:
    """End-to-end mock: every ``*_encrypted`` column introduced by
    migrations 001-022 is discovered, and the purpose parsed from the
    column comment matches the cipher purpose the storage write path
    uses. This is the regression guard: a future migration that adds
    an ``*_encrypted`` column without setting its comment will fail
    closed (see the next test)."""
    rows = [
        {"table_name": t, "column_name": c, "comment": f"purpose={p}"}
        for (t, c), p in EXPECTED_PURPOSES.items()
    ]
    conn = _make_fake_conn(rows)
    targets = discover_encrypted_columns(conn)
    assert len(targets) == len(EXPECTED_PURPOSES)
    for (table, column), purpose in EXPECTED_PURPOSES.items():
        assert RotationTarget(table=table, column=column, purpose=purpose) in targets


def test_discover_fails_closed_when_a_column_has_no_purpose_comment() -> None:
    """The fail-closed contract: a column with no ``purpose=...``
    comment causes ``discover_encrypted_columns`` to raise
    ``ValueError`` listing the missing registration. Rotation refuses
    to run rather than silently skip the column.

    This is the regression guard the Founder's brief asked for
    ("This is a structural fix, not a prompt fix. It catches future
    columns automatically.") — combined with the column-comment
    convention from migration 022, it makes it impossible to ship a
    new ``*_encrypted`` column without declaring its AAD purpose.
    """
    rows = [
        {
            "table_name": "future_table",
            "column_name": "secret_encrypted",
            "comment": None,  # missing purpose comment
        }
    ]
    conn = _make_fake_conn(rows)
    with pytest.raises(ValueError, match="purpose"):
        discover_encrypted_columns(conn)


def test_discover_fails_closed_when_a_column_has_a_non_purpose_comment() -> None:
    """A column comment that does not start with ``purpose=`` is
    treated as missing — the fail-closed contract applies to
    unrelated comments too."""
    rows = [
        {
            "table_name": "future_table",
            "column_name": "secret_encrypted",
            "comment": "classified: top secret",  # not a purpose comment
        }
    ]
    conn = _make_fake_conn(rows)
    with pytest.raises(ValueError, match="purpose"):
        discover_encrypted_columns(conn)


def test_discover_purpose_override_wins_for_tests() -> None:
    """The optional ``purposes`` override lets tests mock the comment
    introspection without a real DB. The override wins over the
    schema comment so the test framework can inject any purpose
    mapping it wants."""
    rows = [
        {
            "table_name": "history_entries",
            "column_name": "text_encrypted",
            "comment": "purpose=history.text",
        }
    ]
    conn = _make_fake_conn(rows)
    targets = discover_encrypted_columns(
        conn,
        purposes={("history_entries", "text_encrypted"): "test.override"},
    )
    assert targets == [
        RotationTarget(
            table="history_entries",
            column="text_encrypted",
            purpose="test.override",
        )
    ]


def test_discover_extracts_only_the_purpose_prefix() -> None:
    """A comment like ``purpose=history.text; classification=secret``
    parses to ``history.text`` — the regex stops at the first
    non-purpose character. This future-proofs the comment format
    against additional metadata fields."""
    rows = [
        {
            "table_name": "history_entries",
            "column_name": "text_encrypted",
            "comment": "purpose=history.text; classification=secret",
        }
    ]
    conn = _make_fake_conn(rows)
    targets = discover_encrypted_columns(conn)
    assert targets == [
        RotationTarget(
            table="history_entries",
            column="text_encrypted",
            purpose="history.text",
        )
    ]


def test_discover_validates_identifiers_defense_in_depth() -> None:
    """Identifiers returned by information_schema are validated against
    a strict regex before SQL interpolation (vision-security Finding 5).
    A row whose table_name contains a quote or semicolon must raise
    ``ValueError`` rather than reach the SQL layer.
    """
    rows = [
        {
            "table_name": "history_entries; DROP TABLE students; --",
            "column_name": "text_encrypted",
            "comment": "purpose=history.text",
        }
    ]
    conn = _make_fake_conn(rows)
    with pytest.raises(ValueError, match="invalid table"):
        discover_encrypted_columns(conn)


# ---------------------------------------------------------------------------
# Postgres-marked: verify the column comments set by migration 022 +
# 018 + 021 actually agree with the cipher purposes the storage write
# path uses. This is the end-to-end proof that the structural fix is
# correct — no Python-side hardcoded list, only the schema + comments.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_postgres_discover_picks_up_every_storage_write_purpose(
    pg_conn: Any,
) -> None:
    """End-to-end: against a fully-migrated database, every
    ``*_encrypted`` column is discovered and its purpose matches the
    cipher purpose the storage layer uses at write time.

    This test does what the brief asked for — "verify it picks up the
    new columns" — against a real DB. The mocked tests above prove the
    parsing logic; this test proves the migration comments are
    correctly written.
    """
    targets = discover_encrypted_columns(pg_conn)
    discovered = {(t.table, t.column): t.purpose for t in targets}
    for (table, column), purpose in EXPECTED_PURPOSES.items():
        assert (table, column) in discovered, (
            f"migration did not register a purpose comment for "
            f"{table}.{column} — the structural fix did not pick it up"
        )
        assert discovered[(table, column)] == purpose, (
            f"purpose mismatch on {table}.{column}: "
            f"expected {purpose!r}, got {discovered[(table, column)]!r}"
        )


@pytest.mark.postgres
def test_postgres_discover_includes_learning_states_and_calendar(
    pg_conn: Any,
) -> None:
    """The two new tables introduced by this work — ``learning_states``
    and ``student_calendar`` — must be in the discovered rotation
    inventory. Without the structural fix, their ``*_encrypted``
    columns would have been silently orphaned during key rotation
    (the bug the Founder's brief called out).
    """
    targets = discover_encrypted_columns(pg_conn)
    tables = {t.table for t in targets}
    assert "learning_states" in tables, (
        "learning_states.payload_encrypted must be in the rotation "
        "inventory — migration 018 column comment is required"
    )
    assert "student_calendar" in tables, (
        "student_calendar.notes_encrypted must be in the rotation "
        "inventory — migration 021 column comment is required"
    )


# ---------------------------------------------------------------------------
# Sanity: the discover function never reads student data
# ---------------------------------------------------------------------------


def test_discover_query_does_not_reference_student_columns() -> None:
    """The introspection query must read only structural metadata
    (column names, table RLS flag, column comments). It must never
    SELECT student-data columns (e.g. ``payload_encrypted`` itself,
    ``text_encrypted`` itself) — only the ``information_schema`` view
    and ``pg_description``.

    This is the same contract as ``waxprep_orphan_student_ids`` and
    ``waxprep_students_with_due_jobs``: cross-tenant introspection
    that returns only metadata, never student content.
    """
    captured_sql: list[str] = []

    class _CapturingCursor:
        def __enter__(self) -> _CapturingCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(
            self, sql: str, params: tuple[Any, ...] | None = None
        ) -> None:
            captured_sql.append(sql)

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    class _CapturingConn:
        def cursor(self, row_factory: Any = None) -> _CapturingCursor:
            return _CapturingCursor()

    discover_encrypted_columns(_CapturingConn())  # type: ignore[arg-type]
    # The first captured statement is "RESET ROLE" (so the introspection
    # can read pg_class / information_schema freely); the actual
    # metadata query is the second.
    assert len(captured_sql) >= 2
    sql = captured_sql[1]
    # The query must reference information_schema.columns, not a
    # student-data table directly.
    assert "information_schema.columns" in sql
    # And must never read student data columns by name (those are the
    # columns the rotation later decrypts — the discover step must
    # only inspect metadata).
    forbidden = (
        "payload_encrypted",
        "text_encrypted",
        "notes_encrypted",
        "quote_encrypted",
        "filename_encrypted",
        "vector_encrypted",
        "node_type_encrypted",
        "label_encrypted",
        "description_encrypted",
        "properties_encrypted",
        "relation_encrypted",
        "input_encrypted",
        "result_encrypted",
    )
    for col in forbidden:
        # The column name may legitimately appear as a LIKE pattern
        # suffix (``%_encrypted``), but never as a bare SELECT column
        # (``SELECT payload_encrypted FROM ...``).
        assert f"SELECT {col}" not in sql
        assert f"SELECT {col}," not in sql
        assert f", {col} FROM" not in sql


# Suppress unused-import warnings for fixtures used only by
# postgres-marked tests.
_ = (MagicMock, datetime(2026, 1, 1, tzinfo=UTC))
