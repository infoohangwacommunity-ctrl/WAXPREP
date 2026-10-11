"""Background work fails and retries without ever endangering history.

Blocker B acceptance tests (spec §8.3 item 3):
(a) no embedding job is enqueued when the embedder is absent,
(b) token indexing still happens (now atomically with the history row),
(c) enabling embeddings plus the backfill command eventually indexes
    earlier objects.

Plus the original durability tests:
- failed embedding is retried later and history stays,
- a job gives up after max attempts but history stays.
"""

from __future__ import annotations

import pytest

from support import TEST_CIPHER, add_student, make_world
from waxprep.domain.models import AuthorKind, JobStatus, ObjectType
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.index.indexer import JOB_EMBED
from waxprep.index.tokens import TokenIndex
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


# --- Blocker B: capability-aware indexer -----------------------------------

@pytest.mark.asyncio
async def test_no_embedding_job_enqueued_when_embedder_absent() -> None:
    """(a) No JOB_EMBED is enqueued when no embedder is configured."""
    storage = InMemoryStorage()
    world = make_world(storage=storage)  # no embedder
    wax = add_student(world)
    world.services.history.record(wax, AuthorKind.STUDENT, "queued only")
    jobs = storage.list_jobs(wax)
    assert all(j.kind != JOB_EMBED for j in jobs), (
        "no embedding job may be enqueued when no embedder is configured"
    )


def test_token_indexing_runs_without_embedder() -> None:
    """(b) Token indexing happens, atomically with the history row.

    With the atomic history+tokens write (Blocker C), tokens are written
    in the same transaction as the history row — there is no separate
    JOB_INDEX_TOKENS to run. Verify the index is populated.
    """
    storage = InMemoryStorage()
    world = make_world(storage=storage)
    wax = add_student(world)
    entry = world.services.history.record(
        wax, AuthorKind.STUDENT, "Adeyemi worksheet seven"
    )
    # Tokens should already be in the index, even though no worker ran.
    index = TokenIndex(TEST_CIPHER)
    hashes = index.hashes(wax, "adeyemi")
    matches = storage.find_by_tokens(
        wax,
        hashes,
        object_types=(ObjectType.HISTORY,),
        since=None,
        until=None,
        limit=10,
    )
    assert any(m.object_id == entry.id for m in matches), (
        "token indexing must happen even without an embedder"
    )


@pytest.mark.asyncio
async def test_backfill_indexes_objects_after_embedder_added() -> None:
    """(c) Adding an embedder + running backfill eventually embeds old objects."""
    storage = InMemoryStorage()
    # First stage: no embedder configured.
    world = make_world(storage=storage)
    wax = add_student(world)
    world.services.history.record(
        wax, AuthorKind.STUDENT, "old message saved without embeddings"
    )
    # Sanity: no embedding job yet.
    jobs = storage.list_jobs(wax)
    assert all(j.kind != JOB_EMBED for j in jobs)
    # Second stage: an embedder is added; run the backfill command.
    new_world = make_world(
        storage=storage, embedder=DeterministicEmbeddingProvider(dimensions=8)
    )
    history = [
        e for e in storage.list_history(wax, limit=50)
        if e.text and e.text.strip()
    ]
    objects = tuple(
        (ObjectType.HISTORY, e.id, e.occurred_at) for e in history
    )
    enqueued = new_world.services.indexer.backfill_embedding_jobs(wax, objects)
    assert enqueued >= 1
    # The new embedding jobs are now due and run.
    report = await new_world.services.worker.run_once()
    assert report.done >= 1
    embeddings = storage.list_embeddings(
        wax,
        (ObjectType.HISTORY,),
        since=None,
        until=None,
        limit=10,
    )
    assert embeddings, "backfill must eventually embed the old object"
