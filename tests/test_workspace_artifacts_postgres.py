"""PostgreSQL Build 2.5 Workspace/Artifact isolation tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Artifact,
    ArtifactStatus,
    ArtifactVersion,
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
    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(
            """
            DROP TABLE IF EXISTS
                artifact_references, artifact_versions, artifacts, workspaces,
                events, notebook_entries, notebooks, attachments, messages,
                conversations, channel_identities, students, schema_migrations
            CASCADE
            """
        )
        cur.execute("DROP FUNCTION IF EXISTS waxprep_lookup_channel(TEXT, TEXT)")
        cur.execute("DROP FUNCTION IF EXISTS waxprep_current_wax_id()")
    conn.commit()
    apply_migrations(conn)
    yield PostgresStorage(conn)
    conn.close()


def test_workspace_artifact_version_chain(pg_store: PostgresStorage) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    student = Student(
        wax_id=new_wax_id(),
        created_at=now,
        updated_at=now,
        status=StudentStatus.ACTIVE,
    )
    pg_store.create_student(student)
    workspace = pg_store.get_or_create_workspace(student.wax_id, now)
    artifact = Artifact(
        id=new_id(),
        wax_id=student.wax_id,
        workspace_id=workspace.id,
        status=ArtifactStatus.ACTIVE,
        current_version_id=None,
        created_at=now,
        updated_at=now,
    )
    pg_store.create_artifact(artifact)
    version = ArtifactVersion(
        id=new_id(),
        wax_id=student.wax_id,
        artifact_id=artifact.id,
        version_number=1,
        storage_ref="test://artifact/1",
        original_filename="notes.pdf",
        mime_type="application/pdf",
        size_bytes=123,
        checksum_sha256="a" * 64,
        created_at=now,
    )
    pg_store.create_artifact_version(version)
    updated = pg_store.set_current_artifact_version(
        student.wax_id, artifact.id, version.id, now
    )
    assert updated.current_version_id == version.id
    loaded = pg_store.get_artifact(student.wax_id, artifact.id)
    assert loaded is not None
    assert loaded.current_version_id == version.id


def test_workspace_rls_blocks_other_student(pg_store: PostgresStorage) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    student_a = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    student_b = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    pg_store.create_student(student_a)
    pg_store.create_student(student_b)
    workspace_a = pg_store.get_or_create_workspace(student_a.wax_id, now)
    artifact = Artifact(
        id=new_id(),
        wax_id=student_a.wax_id,
        workspace_id=workspace_a.id,
        status=ArtifactStatus.ACTIVE,
        current_version_id=None,
        created_at=now,
        updated_at=now,
    )
    pg_store.create_artifact(artifact)
    assert pg_store.get_workspace(student_b.wax_id) is None
    assert pg_store.get_artifact(student_b.wax_id, artifact.id) is None
    assert pg_store.list_artifacts(student_b.wax_id, workspace_a.id) == ()


def test_artifact_version_isolation(pg_store: PostgresStorage) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    student_a = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    student_b = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    pg_store.create_student(student_a)
    pg_store.create_student(student_b)
    workspace_a = pg_store.get_or_create_workspace(student_a.wax_id, now)
    artifact = Artifact(
        id=new_id(),
        wax_id=student_a.wax_id,
        workspace_id=workspace_a.id,
        status=ArtifactStatus.ACTIVE,
        current_version_id=None,
        created_at=now,
        updated_at=now,
    )
    pg_store.create_artifact(artifact)
    version = ArtifactVersion(
        id=new_id(),
        wax_id=student_a.wax_id,
        artifact_id=artifact.id,
        version_number=1,
        storage_ref="test://private/a",
        original_filename="private.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        checksum_sha256=None,
        created_at=now,
    )
    pg_store.create_artifact_version(version)
    assert (
        pg_store.get_artifact_version(student_b.wax_id, artifact.id, version.id) is None
    )
    assert pg_store.list_artifact_versions(student_b.wax_id, artifact.id) == ()
