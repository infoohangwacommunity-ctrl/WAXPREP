"""Blocker G acceptance tests (spec §8.3 item 10):
- A derived fact with tutor-only source evidence stays marked as
  requiring student confirmation when it passes through summaries or
  notebook entries. The flag must not be lost through derivation layers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from support import TEST_CIPHER, add_student, make_world
from waxprep.context.evolution import EvolutionService, NEEDS_CONFIRMATION
from waxprep.context.proposals import EvidenceRef, Operation, Proposal
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    ObjectType,
    ProvenanceLink,
    Summary,
    SummaryStatus,
    new_id,
)
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.storage.memory import InMemoryStorage

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _tutor_only_summary_chain(world, wax: WaxId) -> tuple[HistoryEntry, Summary]:
    """Build: tutor history entry → summary (linked via provenance)."""
    # 1. Tutor says something (no student evidence).
    tutor_entry = world.services.history.record(
        wax, AuthorKind.TUTOR, "You should study at 6am every day."
    )
    # 2. A summary is created that cites the tutor entry.
    summary = world.storage.add_summary(
        Summary(
            id=new_id(),
            wax_id=wax,
            text="Recommendation: morning study routine.",
            covers_from=NOW,
            covers_until=NOW,
            level=1,
            status=SummaryStatus.ACTIVE,
            produced_by="consolidation",
            created_at=NOW,
        )
    )
    world.storage.add_provenance_links(
        (
            ProvenanceLink(
                id=new_id(),
                wax_id=wax,
                derived_type=ObjectType.SUMMARY,
                derived_id=summary.id,
                source_type=ObjectType.HISTORY,
                source_id=tutor_entry.id,
                source_author_kind=AuthorKind.TUTOR,
                quote="You should study at 6am",
                created_at=NOW,
            ),
        )
    )
    return tutor_entry, summary


def test_tutor_only_fact_through_summary_still_needs_confirmation() -> None:
    """§8.3 item 10: A derived fact with tutor-only source evidence stays
    marked as requiring student confirmation when it passes through a summary.

    Without recursive resolution, the proposal's only immediate evidence is
    a SUMMARY (not HISTORY), so the original `_needs_confirmation` would
    return True only when evidence was exclusively HISTORY — but it would
    fail to detect that the summary's lineage is tutor-only.
    """
    world = make_world()
    wax = add_student(world)
    _, summary = _tutor_only_summary_chain(world, wax)
    # A new notebook entry cites the summary (which cites only tutor history).
    proposal = Proposal(
        wax_id=wax,
        operation=Operation.ADD_NOTEBOOK_ENTRY,
        evidence=(EvidenceRef(ObjectType.SUMMARY, summary.id, "morning routine"),),
        payload={
            "schema_version": 1,
            "payload": {"note": "student should study in the morning"},
        },
    )
    result = world.services.evolution.apply(proposal, now=NOW)
    assert result.accepted, f"proposal must be accepted; got: {result.reason}"
    notebook_entry = world.storage.get_notebook_entry(wax, result.created_notebook_entry_id)
    assert notebook_entry is not None
    assert notebook_entry.payload.get(NEEDS_CONFIRMATION) is True, (
        "tutor-only fact passed through a summary must keep the "
        "'needs_student_confirmation' flag"
    )


def test_student_fact_through_summary_does_not_need_confirmation() -> None:
    """The mirror case: a summary whose lineage includes a student fact
    should NOT be marked as needing confirmation."""
    world = make_world()
    wax = add_student(world)
    student_entry = world.services.history.record(
        wax, AuthorKind.STUDENT, "I prefer studying in the morning."
    )
    summary = world.storage.add_summary(
        Summary(
            id=new_id(),
            wax_id=wax,
            text="Student prefers morning study.",
            covers_from=NOW,
            covers_until=NOW,
            level=1,
            status=SummaryStatus.ACTIVE,
            produced_by="consolidation",
            created_at=NOW,
        )
    )
    world.storage.add_provenance_links(
        (
            ProvenanceLink(
                id=new_id(),
                wax_id=wax,
                derived_type=ObjectType.SUMMARY,
                derived_id=summary.id,
                source_type=ObjectType.HISTORY,
                source_id=student_entry.id,
                source_author_kind=AuthorKind.STUDENT,
                quote="I prefer studying in the morning",
                created_at=NOW,
            ),
        )
    )
    proposal = Proposal(
        wax_id=wax,
        operation=Operation.ADD_NOTEBOOK_ENTRY,
        evidence=(EvidenceRef(ObjectType.SUMMARY, summary.id, "morning preference"),),
        payload={
            "schema_version": 1,
            "payload": {"note": "student prefers morning"},
        },
    )
    result = world.services.evolution.apply(proposal, now=NOW)
    assert result.accepted
    notebook_entry = world.storage.get_notebook_entry(wax, result.created_notebook_entry_id)
    assert notebook_entry is not None
    assert notebook_entry.payload.get(NEEDS_CONFIRMATION) is not True, (
        "student-authored fact through a summary must NOT be marked as "
        "needing confirmation"
    )


def test_provenance_cycle_does_not_infinite_loop() -> None:
    """A pathological cycle in provenance_links (A → B → A) must not cause
    infinite recursion."""
    world = make_world()
    wax = add_student(world)
    storage = world.storage
    # Create two summaries that cite each other (a cycle).
    s1 = storage.add_summary(
        Summary(
            id=new_id(), wax_id=wax, text="A", covers_from=NOW, covers_until=NOW,
            level=1, status=SummaryStatus.ACTIVE, produced_by="t", created_at=NOW,
        )
    )
    s2 = storage.add_summary(
        Summary(
            id=new_id(), wax_id=wax, text="B", covers_from=NOW, covers_until=NOW,
            level=1, status=SummaryStatus.ACTIVE, produced_by="t", created_at=NOW,
        )
    )
    storage.add_provenance_links(
        (
            ProvenanceLink(
                id=new_id(), wax_id=wax,
                derived_type=ObjectType.SUMMARY, derived_id=s1.id,
                source_type=ObjectType.SUMMARY, source_id=s2.id,
                created_at=NOW,
            ),
            ProvenanceLink(
                id=new_id(), wax_id=wax,
                derived_type=ObjectType.SUMMARY, derived_id=s2.id,
                source_type=ObjectType.SUMMARY, source_id=s1.id,
                created_at=NOW,
            ),
        )
    )
    # Resolve through the cycle — must terminate.
    service: EvolutionService = world.services.evolution
    authors = service._resolve_to_history_authors(  # type: ignore[attr-defined]
        wax, ObjectType.SUMMARY, s1.id
    )
    assert authors == [], (
        "a cycle with no history root must terminate and return no authors"
    )
