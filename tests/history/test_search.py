"""Finding things: exact words, meaning, time, and student isolation."""

from __future__ import annotations

from datetime import timedelta

import pytest

from support import World, add_student, make_world
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, ObjectType
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.index.tokens import normalize_tokens


def _say(
    world: World, wax: WaxId, text: str, author: AuthorKind = AuthorKind.STUDENT
) -> str:
    return str(world.services.history.record(wax, author, text).id)


def _exact(world: World, wax: WaxId, query: str, **kwargs: object) -> list[str]:
    hits = world.services.retriever.find_exact(
        wax,
        query,
        now=world.clock.now(),
        **kwargs,  # type: ignore[arg-type]
    )
    return [h.content for h in hits]


def test_tokenizer_is_mechanical_and_accent_blind() -> None:
    assert normalize_tokens("Ọmọ, MATHS! Q5 12x²") == ("omo", "maths", "q5", "12x2")


def test_exact_search_finds_specific_name_and_number() -> None:
    world = make_world()
    wax = add_student(world)
    _say(world, wax, "Mr Adeyemi gave us worksheet 7 on Friday")
    _say(world, wax, "unrelated chat about football")
    assert _exact(world, wax, "Adeyemi worksheet 7")[0].startswith("Mr Adeyemi")


def test_very_old_messages_are_still_found() -> None:
    world = make_world()
    wax = add_student(world)
    _say(world, wax, "I told you I am always hungry after school")
    world.clock.advance(3 * 365 * 86400)  # three years
    assert _exact(world, wax, "hungry after school")  # age lowers rank, never hides


def test_common_words_are_discovered_from_data_not_a_stoplist() -> None:
    world = make_world()
    wax = add_student(world)
    for i in range(12):
        _say(world, wax, f"the thing number {i} is here")
    _say(world, wax, "the photosynthesis experiment")
    assert (
        _exact(world, wax, "the photosynthesis")[0] == "the photosynthesis experiment"
    )


def test_time_range_limits_results() -> None:
    world = make_world()
    wax = add_student(world)
    _say(world, wax, "exam preparation plan")
    mark = world.clock.now() + timedelta(days=1)
    world.clock.advance(2 * 86400)
    _say(world, wax, "exam preparation update")
    later = _exact(world, wax, "exam", since=mark)
    assert later == ["exam preparation update"]


def test_search_never_crosses_students() -> None:
    world = make_world()
    a, b = add_student(world), add_student(world)
    _say(world, a, "alpha secret phrase")
    _say(world, b, "beta secret phrase")
    assert all("alpha" in c for c in _exact(world, a, "secret phrase"))
    assert all("beta" in c for c in _exact(world, b, "secret phrase"))


def test_tutor_words_are_marked_as_tutor_evidence() -> None:
    world = make_world()
    wax = add_student(world)
    _say(world, wax, "You are preparing for WAEC", AuthorKind.TUTOR)
    hit = world.services.retriever.find_exact(wax, "WAEC", now=world.clock.now())[0]
    assert hit.author_kind == "tutor"
    assert any("not proof" in note for note in hit.provenance)


@pytest.mark.asyncio
async def test_meaning_search_works_after_background_embedding() -> None:
    embedder = DeterministicEmbeddingProvider(dimensions=32)
    world = make_world(embedder=embedder)
    wax = add_student(world)
    _say(world, wax, "completing the square")
    _say(world, wax, "my favourite football club")
    report = await world.services.worker.run_once()
    assert report.done >= 2
    vector = await embedder.embed("completing the square")
    hits = world.services.retriever.find_by_meaning(wax, vector, now=world.clock.now())
    assert hits and hits[0].content == "completing the square"


def test_summaries_notebook_and_artifacts_share_the_same_index() -> None:
    world = make_world()
    wax = add_student(world)
    entry_id = world.services.history.record(wax, AuthorKind.STUDENT, "alpha").id
    found = world.services.retriever.inspect(wax, entry_id)
    assert found and found[0].evidence_type.value == ObjectType.HISTORY.value
