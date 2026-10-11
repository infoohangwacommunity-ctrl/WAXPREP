"""Background work fails and retries without ever endangering history."""

from __future__ import annotations

import pytest

from support import add_student, make_world
from waxprep.domain.models import AuthorKind, JobStatus
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.storage.memory import InMemoryStorage


class FlakyEmbedder(DeterministicEmbeddingProvider):
    def __init__(self, failures: int) -> None:
        super().__init__(dimensions=8)
        self.failures = failures

    async def embed_many(self, texts: list[str]) -> list[tuple[float, ...]]:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("embedding provider down")
        return await super().embed_many(texts)


@pytest.mark.asyncio
async def test_failed_embedding_is_retried_later_and_message_stays() -> None:
    storage = InMemoryStorage()
    world = make_world(storage=storage, embedder=FlakyEmbedder(failures=1))
    wax = add_student(world)
    entry = world.services.history.record(wax, AuthorKind.STUDENT, "remember me")
    first = await world.services.worker.run_once()
    assert first.retrying == 1
    assert storage.get_history_entry(wax, entry.id) is not None
    world.clock.advance(3600)  # past the retry delay
    second = await world.services.worker.run_once()
    assert second.done >= 1
    assert all(j.status is JobStatus.DONE for j in storage.list_jobs(wax))


@pytest.mark.asyncio
async def test_job_gives_up_after_max_attempts_but_history_survives() -> None:
    storage = InMemoryStorage()
    world = make_world(storage=storage, embedder=FlakyEmbedder(failures=99))
    wax = add_student(world)
    entry = world.services.history.record(wax, AuthorKind.STUDENT, "still here")
    for _ in range(6):
        await world.services.worker.run_once()
        world.clock.advance(86400)
    assert any(j.status is JobStatus.FAILED for j in storage.list_jobs(wax))
    assert storage.get_history_entry(wax, entry.id) is not None


@pytest.mark.asyncio
async def test_jobs_without_a_configured_capability_stay_queued() -> None:
    storage = InMemoryStorage()
    world = make_world(storage=storage)  # no embedder configured
    wax = add_student(world)
    world.services.history.record(wax, AuthorKind.STUDENT, "queued only")
    report = await world.services.worker.run_once()
    assert report.skipped >= 1
    assert any(j.status is JobStatus.PENDING for j in storage.list_jobs(wax))
