"""PostgreSQL Workspace/Artifact isolation and encrypted filenames."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Artifact,
    ArtifactContent,
    ArtifactStatus,
    ArtifactVersion,
    Student,
    new_id,
)
from waxprep.storage.postgres import PostgresStorage

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _version(
    store: PostgresStorage, name: str | None = "biology_notes.pdf"
) -> tuple[Any, Artifact, ArtifactVersion]:
    wax = new_wax_id()
    store.create_student(Student(wax, NOW, NOW))
    workspace = store.get_or_create_workspace(wax, NOW)
    artifact = store.create_artifact(
        Artifact(new_id(), wax, workspace.id, ArtifactStatus.ACTIVE, None, NOW, NOW)
    )
    version = store.create_artifact_version(
        ArtifactVersion(
            new_id(),
            wax,
            artifact.id,
            1,
            "test://a/1",
            name,
            "application/pdf",
            123,
            None,
            NOW,
        )
    )
    return wax, artifact, version


def test_version_chain_and_current_pointer(pg_store: PostgresStorage) -> None:
    wax, artifact, version = _version(pg_store)
    updated = pg_store.set_current_artifact_version(wax, artifact.id, version.id, NOW)
    assert updated.current_version_id == version.id
    loaded = pg_store.get_artifact_version_by_id(wax, version.id)
    assert loaded is not None and loaded.original_filename == "biology_notes.pdf"


def test_filename_is_encrypted_at_rest(
    pg_store: PostgresStorage, pg_conn: psycopg.Connection[Any]
) -> None:
    _, _, version = _version(pg_store)
    with pg_conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT filename_encrypted FROM artifact_versions WHERE id = %s",
            (version.id,),
        )
        raw = bytes(cur.fetchone()[0])  # type: ignore[index]
    assert b"biology" not in raw


def test_artifact_content_round_trip_and_isolation(pg_store: PostgresStorage) -> None:
    wax, artifact, version = _version(pg_store)
    other = new_wax_id()
    pg_store.create_student(Student(other, NOW, NOW))
    content = pg_store.add_artifact_content(
        ArtifactContent(
            new_id(), wax, version.id, "readable_text", "Q38 not started", "reader", NOW
        )
    )
    assert pg_store.get_artifact_content(wax, content.id) is not None
    assert pg_store.get_artifact_content(other, content.id) is None
    assert pg_store.get_artifact(other, artifact.id) is None
    assert pg_store.list_artifact_versions(other, artifact.id) == ()
