"""Consolidation compresses for speed; it never deletes or invents."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from support import add_student, make_world
from waxprep.derive.consolidation import (
    ConsolidationError,
    ConsolidationOutput,
    NotebookDraft,
    parse_consolidation,
)
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, HistoryEntry, ObjectType


class FakeConsolidator:
    name = "fake-consolidator"

    def __init__(self, *, fail: bool = False, bogus: bool = True) -> None:
        self.fail = fail
        self.bogus = bogus
        self.last_entries: tuple[HistoryEntry, ...] = ()

    async def consolidate(
        self, wax_id: WaxId, entries: tuple[HistoryEntry, ...], now: datetime
    ) -> ConsolidationOutput:
        self.last_entries = entries
        if self.fail:
            raise ConsolidationError("model down")
        drafts = [
            NotebookDraft({"observation": "struggles with fractions"}, (entries[0].id,))
        ]
        if self.bogus:
            drafts.append(NotebookDraft({"observation": "made up"}, (uuid4(),)))
        return ConsolidationOutput(
            "Student worked on fractions and quadratics.", tuple(drafts)
        )


def _seed(world, wax: WaxId, count: int = 4) -> list[HistoryEntry]:  # type: ignore[no-untyped-def]
    w = world  # type: ignore[assignment]
    out = []
    for i in range(count):
        w.clock.advance(3600)  # type: ignore[attr-defined]
        out.append(
            w.services.history.record(wax, AuthorKind.STUDENT, f"fractions q{i}")
        )  # type: ignore[attr-defined]
    return out


@pytest.mark.asyncio
async def test_summary_links_to_every_covered_entry_and_history_stays() -> None:
    model = FakeConsolidator()
    world = make_world(consolidation_model=model)
    wax = add_student(world)
    entries = _seed(world, wax)
    world.clock.advance(200 * 86400)
    assert world.services.consolidation is not None
    summary = await world.services.consolidation.consolidate_student(
        wax, older_than=timedelta(days=30)
    )
    assert summary is not None
    links = world.storage.list_provenance(wax, ObjectType.SUMMARY, summary.id)
    assert {l.source_id for l in links} == {e.id for e in entries}
    assert len(world.storage.list_history(wax)) == len(entries)  # nothing removed
    traced = world.services.retriever.trace_sources(wax, summary.id, ObjectType.SUMMARY)
    assert len(traced) == len(entries)


@pytest.mark.asyncio
async def test_understanding_that_cites_nothing_real_is_dropped() -> None:
    world = make_world(consolidation_model=FakeConsolidator(bogus=True))
    wax = add_student(world)
    _seed(world, wax)
    world.clock.advance(200 * 86400)
    assert world.services.consolidation is not None
    await world.services.consolidation.consolidate_student(
        wax, older_than=timedelta(days=30)
    )
    notebook = world.storage.get_notebook(wax)
    assert notebook is not None
    notes = world.storage.list_notebook_entries(wax, notebook.id)
    assert [n.payload["observation"] for n in notes] == ["struggles with fractions"]


@pytest.mark.asyncio
async def test_covered_entries_are_not_summarised_twice_and_recent_ones_wait() -> None:
    world = make_world(consolidation_model=FakeConsolidator())
    wax = add_student(world)
    _seed(world, wax)
    assert world.services.consolidation is not None
    too_soon = await world.services.consolidation.consolidate_student(
        wax, older_than=timedelta(days=30)
    )
    assert too_soon is None  # everything is recent
    world.clock.advance(200 * 86400)
    first = await world.services.consolidation.consolidate_student(
        wax, older_than=timedelta(days=30)
    )
    second = await world.services.consolidation.consolidate_student(
        wax, older_than=timedelta(days=30)
    )
    assert first is not None and second is None


@pytest.mark.asyncio
async def test_failed_consolidation_changes_nothing() -> None:
    world = make_world(consolidation_model=FakeConsolidator(fail=True))
    wax = add_student(world)
    _seed(world, wax)
    world.clock.advance(200 * 86400)
    assert world.services.consolidation is not None
    with pytest.raises(ConsolidationError):
        await world.services.consolidation.consolidate_student(
            wax, older_than=timedelta(days=30)
        )
    assert world.storage.list_summaries(wax) == ()
    assert (
        len(
            world.storage.list_unsummarized_history(
                wax, before=world.clock.now(), limit=50
            )
        )
        == 4
    )


def test_model_output_parsing_is_strict_about_shape() -> None:
    entry_id = uuid4()
    parsed = parse_consolidation(
        f'{{"summary": "s", "notebook": [{{"payload": {{"a": 1}}, "supported_by": ["{entry_id}", "junk"]}}]}}'
    )
    assert parsed.summary_text == "s"
    assert parsed.notebook_drafts[0].supporting_entry_ids == (entry_id,)
    with pytest.raises(ConsolidationError):
        parse_consolidation("not json at all")
