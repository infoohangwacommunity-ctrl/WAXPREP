"""Build 2.5 Workspace and Artifact storage contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from waxprep.clock import FakeClock
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Artifact,
    ArtifactReference,
    ArtifactStatus,
    ArtifactVersion,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.storage.memory import InMemoryStorage


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))


@pytest.fixture
def store() -> InMemoryStorage:
    return InMemoryStorage()


def _student(clock: FakeClock) -> Student:
    now = clock.now()
    return Student(
        wax_id=new_wax_id(),
        created_at=now,
        updated_at=now,
        status=StudentStatus.ACTIVE,
    )


def test_workspace_belongs_to_student(store: InMemoryStorage, clock: FakeClock) -> None:
    student = _student(clock)
    store.create_student(student)
    workspace = store.get_or_create_workspace(student.wax_id, clock.now())
    assert workspace.wax_id == student.wax_id
    assert store.get_workspace(student.wax_id) == workspace
    assert store.get_workspace(new_wax_id()) is None


def test_workspace_is_created_only_once(
    store: InMemoryStorage, clock: FakeClock
) -> None:
    student = _student(clock)
    store.create_student(student)
    first = store.get_or_create_workspace(student.wax_id, clock.now())
    clock.advance(10)
    second = store.get_or_create_workspace(student.wax_id, clock.now())
    assert first.id == second.id
    assert first.created_at == second.created_at


def test_artifact_and_versions(store: InMemoryStorage, clock: FakeClock) -> None:
    student = _student(clock)
    store.create_student(student)
    workspace = store.get_or_create_workspace(student.wax_id, clock.now())
    artifact = Artifact(
        id=new_id(),
        wax_id=student.wax_id,
        workspace_id=workspace.id,
        status=ArtifactStatus.ACTIVE,
        current_version_id=None,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.create_artifact(artifact)
    first = ArtifactVersion(
        id=new_id(),
        wax_id=student.wax_id,
        artifact_id=artifact.id,
        version_number=1,
        storage_ref="memory://artifact/1",
        original_filename="question.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        checksum_sha256="a" * 64,
        created_at=clock.now(),
    )
    second = ArtifactVersion(
        id=new_id(),
        wax_id=student.wax_id,
        artifact_id=artifact.id,
        version_number=2,
        storage_ref="memory://artifact/2",
        original_filename="question-corrected.pdf",
        mime_type="application/pdf",
        size_bytes=200,
        checksum_sha256="b" * 64,
        created_at=clock.now(),
    )
    store.create_artifact_version(first)
    store.create_artifact_version(second)
    versions = store.list_artifact_versions(student.wax_id, artifact.id)
    assert [v.version_number for v in versions] == [1, 2]
    updated = store.set_current_artifact_version(
        student.wax_id, artifact.id, second.id, clock.now()
    )
    assert updated.current_version_id == second.id


def test_artifact_cannot_cross_student_boundary(
    store: InMemoryStorage, clock: FakeClock
) -> None:
    student_a = _student(clock)
    student_b = _student(clock)
    store.create_student(student_a)
    store.create_student(student_b)
    workspace_a = store.get_or_create_workspace(student_a.wax_id, clock.now())
    artifact = Artifact(
        id=new_id(),
        wax_id=student_a.wax_id,
        workspace_id=workspace_a.id,
        status=ArtifactStatus.ACTIVE,
        current_version_id=None,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.create_artifact(artifact)
    assert store.get_artifact(student_b.wax_id, artifact.id) is None
    assert store.list_artifacts(student_b.wax_id, workspace_a.id) == ()


def test_artifact_reference_does_not_duplicate_artifact(
    store: InMemoryStorage, clock: FakeClock
) -> None:
    student = _student(clock)
    store.create_student(student)
    workspace = store.get_or_create_workspace(student.wax_id, clock.now())
    artifact = Artifact(
        id=new_id(),
        wax_id=student.wax_id,
        workspace_id=workspace.id,
        status=ArtifactStatus.ACTIVE,
        current_version_id=None,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.create_artifact(artifact)
    message_id = new_id()
    reference = ArtifactReference(
        id=new_id(),
        wax_id=student.wax_id,
        artifact_id=artifact.id,
        reference_type="message",
        reference_id=message_id,
        created_at=clock.now(),
    )
    store.create_artifact_reference(reference)
    references = store.list_artifact_references(student.wax_id, artifact.id)
    assert len(references) == 1
    assert references[0].reference_type == "message"
    assert references[0].reference_id == message_id


def test_unknown_artifact_version_is_rejected(
    store: InMemoryStorage, clock: FakeClock
) -> None:
    student = _student(clock)
    store.create_student(student)
    workspace = store.get_or_create_workspace(student.wax_id, clock.now())
    artifact = Artifact(
        id=new_id(),
        wax_id=student.wax_id,
        workspace_id=workspace.id,
        status=ArtifactStatus.ACTIVE,
        current_version_id=None,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.create_artifact(artifact)
    with pytest.raises(ValueError, match="unknown artifact version"):
        store.set_current_artifact_version(
            student.wax_id, artifact.id, new_id(), clock.now()
        )
