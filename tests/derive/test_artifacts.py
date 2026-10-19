"""Artifacts become searchable without any file-type-specific code."""

from __future__ import annotations

import pytest

from support import World, add_student, make_world
from waxprep.derive.artifacts import ExtractedRepresentation
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactStatus,
    ArtifactVersion,
    JobStatus,
    ObjectType,
    new_id,
)
from waxprep.storage.memory import InMemoryStorage


class FakeProcessor:
    name = "fake-reader"

    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls = 0

    async def extract(
        self, wax_id: WaxId, version: ArtifactVersion
    ) -> tuple[ExtractedRepresentation, ...]:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("reader down")
        return (
            ExtractedRepresentation(
                "readable_text",
                "Q1 answered. Q2 answered. Q38 not started. Cell biology.",
            ),
        )


def _add_version(world: World, wax: WaxId, name: str, mime: str) -> ArtifactVersion:
    now = world.clock.now()
    workspace = world.storage.get_or_create_workspace(wax, now)
    artifact = world.storage.create_artifact(
        Artifact(new_id(), wax, workspace.id, ArtifactStatus.ACTIVE, None, now, now)
    )
    return world.storage.create_artifact_version(
        ArtifactVersion(
            new_id(), wax, artifact.id, 1, "blob://x", name, mime, 10, None, now
        )
    )


@pytest.mark.asyncio
async def test_contents_become_searchable_for_any_media_type() -> None:
    for name, mime in (
        ("bio.pdf", "application/pdf"),
        ("voice.ogg", "audio/ogg"),
        ("photo.jpg", "image/jpeg"),
    ):
        world = make_world(artifact_processor=FakeProcessor())
        wax = add_student(world)
        version = _add_version(world, wax, name, mime)
        assert world.services.artifacts is not None
        world.services.artifacts.on_version_created(version)
        await world.services.worker.run_once()
        hits = world.services.retriever.find_exact(
            wax, "cell biology", now=world.clock.now()
        )
        assert [h.evidence_type.value for h in hits] == ["artifact_content"]
        by_name = world.services.retriever.find_exact(
            wax,
            name.split(".")[0],
            now=world.clock.now(),
            object_types=(ObjectType.ARTIFACT_VERSION,),
        )
        assert by_name and by_name[0].evidence_type.value == "artifact"


@pytest.mark.asyncio
async def test_unfinished_work_can_be_resumed_from_stored_contents() -> None:
    world = make_world(artifact_processor=FakeProcessor())
    wax = add_student(world)
    version = _add_version(world, wax, "quiz.pdf", "application/pdf")
    assert world.services.artifacts is not None
    await world.services.artifacts.understand(wax, version.id)
    contents = world.storage.list_artifact_content(wax, version.id)
    assert "Q38 not started" in contents[0].text


@pytest.mark.asyncio
async def test_reader_failure_is_retried_and_artifact_is_untouched() -> None:
    storage = InMemoryStorage()
    world = make_world(
        storage=storage, artifact_processor=FakeProcessor(fail_first=True)
    )
    wax = add_student(world)
    version = _add_version(world, wax, "notes.pdf", "application/pdf")
    assert world.services.artifacts is not None
    world.services.artifacts.on_version_created(version)
    first = await world.services.worker.run_once()
    assert first.retrying == 1
    assert storage.get_artifact_version_by_id(wax, version.id) is not None
    world.clock.advance(3600)
    await world.services.worker.run_once()
    assert any(j.status is JobStatus.DONE for j in storage.list_jobs(wax))
    assert storage.list_artifact_content(wax, version.id)
