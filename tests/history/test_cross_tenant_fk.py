"""Cross-tenant foreign-key rejection tests (Part 8 Blocker F, spec §8.3 item 9).

RLS scopes reads by WAX ID, but the database must also enforce that no
foreign key points from a row owned by Student A to a row owned by
Student B. These tests attempt to create such cross-student references
and confirm the database rejects them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    ArtifactStatus,
    AuthorKind,
    HistoryEntry,
    NotebookEntry,
    NotebookEntryStatus,
    ObjectType,
    ProvenanceLink,
    Student,
    new_id,
)
from waxprep.storage.postgres import PostgresStorage
from waxprep.storage.session import assume_app_role, set_current_wax_id

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _student(store: PostgresStorage) -> Any:
    wax = new_wax_id()
    store.create_student(Student(wax, NOW, NOW))
    return wax


def _entry(wax: Any, text: str = "x") -> HistoryEntry:
    return HistoryEntry(new_id(), wax, AuthorKind.STUDENT, "text/plain", text, NOW, NOW)


def _attempt_cross_reference(pg_conn, sql: str, params: tuple[Any, ...]) -> None:
    """Run `sql` as waxprep_app with a current_wax_id set, expecting the
    statement to fail because of a cross-tenant FK violation."""
    assume_app_role(pg_conn)
    # Set the role's current_wax_id to the attacker's student so RLS
    # lets the INSERT through on the wax_id column; the cross-tenant FK
    # must still reject it.
    # Caller supplies the attacker's wax_id via params.
    with pg_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(sql, params)
        pg_conn.commit()
    pg_conn.rollback()


def test_history_reply_to_other_student_is_rejected(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    """A history row cannot reply to another student's history row."""
    a = _student(pg_store)
    b = _student(pg_store)
    a_entry = pg_store.append_history_entry(_entry(a, "from a"))
    # b tries to reply to a's entry
    set_current_wax_id(pg_conn, b)
    with pg_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO history_entries (id, wax_id, author_kind, content_kind, "
            "occurred_at, recorded_at, reply_to_id) "
            "VALUES (%s, %s, 'tutor', 'text/plain', %s, %s, %s)",
            (uuid4(), b, NOW, NOW, a_entry.id),
        )
        pg_conn.commit()
    pg_conn.rollback()


def test_attachment_to_other_student_history_is_rejected(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    """An attachment cannot be linked to another student's history row."""
    a = _student(pg_store)
    b = _student(pg_store)
    a_entry = pg_store.append_history_entry(_entry(a, "from a"))
    set_current_wax_id(pg_conn, b)
    with pg_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO attachments (id, wax_id, history_entry_id, "
            "kind, storage_ref, created_at) "
            "VALUES (%s, %s, %s, 'file', 'ref', %s)",
            (uuid4(), b, a_entry.id, NOW),
        )
        pg_conn.commit()
    pg_conn.rollback()


def test_artifact_content_to_other_student_version_is_rejected(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    """artifact_contents cannot reference a version owned by another student."""
    a = _student(pg_store)
    b = _student(pg_store)
    # Create a workspace + artifact + version for student a.
    workspace_id = uuid4()
    set_current_wax_id(pg_conn, a)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspaces (id, wax_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (workspace_id, a, NOW, NOW),
        )
        artifact_id = uuid4()
        cur.execute(
            "INSERT INTO artifacts (id, wax_id, workspace_id, status, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (artifact_id, a, workspace_id, ArtifactStatus.ACTIVE.value, NOW, NOW),
        )
        version_id = uuid4()
        cur.execute(
            "INSERT INTO artifact_versions (id, wax_id, artifact_id, "
            "version_number, storage_ref, mime_type, size_bytes, "
            "checksum_sha256, created_at) "
            "VALUES (%s, %s, %s, 1, 'ref', 'text/plain', 1, 'sha', %s)",
            (version_id, a, artifact_id, NOW),
        )
        pg_conn.commit()
    # b tries to add content to a's version.
    set_current_wax_id(pg_conn, b)
    with pg_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO artifact_contents (id, wax_id, artifact_version_id, "
            "representation, text_encrypted, produced_by, created_at) "
            "VALUES (%s, %s, %s, 'text', %s, 'mock', %s)",
            (uuid4(), b, version_id, b"bytes", NOW),
        )
        pg_conn.commit()
    pg_conn.rollback()


def test_investigation_trigger_to_other_student_history_is_rejected(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    """context_investigations.trigger_entry_id cannot point to another
    student's history row."""
    a = _student(pg_store)
    b = _student(pg_store)
    a_entry = pg_store.append_history_entry(_entry(a, "from a"))
    set_current_wax_id(pg_conn, b)
    with pg_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO context_investigations (id, wax_id, trigger_entry_id, "
            "input_encrypted, result_encrypted, model, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'mock', %s)",
            (uuid4(), b, a_entry.id, b"x", b"x", NOW),
        )
        pg_conn.commit()
    pg_conn.rollback()


def test_notebook_supersession_to_other_student_is_rejected(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    """notebook_entries.superseded_by_id cannot point to another student's
    notebook entry."""
    a = _student(pg_store)
    b = _student(pg_store)
    # Create a notebook + entry for student a.
    set_current_wax_id(pg_conn, a)
    notebook_id = uuid4()
    a_entry_id = uuid4()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notebooks (id, wax_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (notebook_id, a, NOW, NOW),
        )
        cur.execute(
            "INSERT INTO notebook_entries (id, wax_id, notebook_id, "
            "schema_version, payload_encrypted, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, 1, %s, 'active', %s, %s)",
            (a_entry_id, a, notebook_id, b"x", NOW, NOW),
        )
        pg_conn.commit()
    # b tries to supersede with a_entry_id pointing to a's entry.
    set_current_wax_id(pg_conn, b)
    b_notebook_id = uuid4()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notebooks (id, wax_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (b_notebook_id, b, NOW, NOW),
        )
        with pytest.raises(psycopg.Error):
            cur.execute(
                "INSERT INTO notebook_entries (id, wax_id, notebook_id, "
                "schema_version, payload_encrypted, status, superseded_by_id, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, 1, %s, 'active', %s, %s, %s)",
                (uuid4(), b, b_notebook_id, b"x", a_entry_id, NOW, NOW),
            )
            pg_conn.commit()
    pg_conn.rollback()


def test_knowledge_edge_to_other_student_node_is_rejected(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    """A knowledge edge cannot point to another student's node."""
    a = _student(pg_store)
    b = _student(pg_store)
    # Create a node for student a.
    set_current_wax_id(pg_conn, a)
    a_node_id = uuid4()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO knowledge_nodes (id, wax_id, node_type_encrypted, "
            "label_encrypted, properties_encrypted, status, created_at, "
            "updated_at) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)",
            (a_node_id, a, b"x", b"x", b"{}", NOW, NOW),
        )
        pg_conn.commit()
    # Create a node for student b.
    set_current_wax_id(pg_conn, b)
    b_node_id = uuid4()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO knowledge_nodes (id, wax_id, node_type_encrypted, "
            "label_encrypted, properties_encrypted, status, created_at, "
            "updated_at) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)",
            (b_node_id, b, b"x", b"x", b"{}", NOW, NOW),
        )
        pg_conn.commit()
    # b tries to create an edge from b's node to a's node.
    set_current_wax_id(pg_conn, b)
    with pg_conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO knowledge_edges (id, wax_id, source_node_id, "
            "target_node_id, relation_encrypted, properties_encrypted, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (uuid4(), b, b_node_id, a_node_id, b"x", b"{}", NOW),
        )
        pg_conn.commit()
    pg_conn.rollback()


def test_provenance_link_service_rejects_cross_student_source(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    """Polymorphic provenance links cannot be created where the derived
    object is owned by one student and the source by another. Polymorphic
    references cannot be protected by an ordinary FK, so the service must
    check ownership and reject."""
    a = _student(pg_store)
    b = _student(pg_store)
    a_entry = pg_store.append_history_entry(_entry(a, "from a"))
    # b creates a notebook entry, then tries to link it to a's history entry.
    nb_entry = pg_store.add_notebook_entry(
        NotebookEntry(
            id=new_id(),
            wax_id=b,
            notebook_id=pg_store.get_or_create_notebook(b, NOW).id,
            schema_version=1,
            payload={"k": "v"},
            status=NotebookEntryStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    link = ProvenanceLink(
        id=new_id(),
        wax_id=b,
        derived_type=ObjectType.NOTEBOOK,
        derived_id=nb_entry.id,
        source_type=ObjectType.HISTORY,
        source_id=a_entry.id,  # cross-student!
        source_author_kind=AuthorKind.STUDENT,
        quote="from a",
        created_at=NOW,
    )
    with pytest.raises((ValueError, psycopg.Error)):
        pg_store.add_provenance_links((link,))
