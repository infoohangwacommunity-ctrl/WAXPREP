"""Acceptance tests for the evidence-led context blueprint.

Blueprint Part 10: tests that verify the system can find relevant older
evidence without a fixed turn window. Each scenario tests a specific
continuity/intelligence capability.

These are component tests using scripted MockContextModel — no live AI
calls. They verify that the APPLICATION behaves correctly when CI makes
the expected investigation decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from support import TEST_CIPHER
from waxprep.context.evolution import EvolutionService
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ContextModelFinal
from waxprep.context.models import InvestigationRequest
from waxprep.context.proposals import EvidenceRef, Operation, Proposal
from waxprep.context.tool_impls import build_default_tools
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    ObjectType,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.storage.memory import InMemoryStorage

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


def _setup_student(storage):
    wax_id = new_wax_id()
    storage.create_student(
        Student(
            wax_id=wax_id,
            created_at=NOW,
            updated_at=NOW,
            status=StudentStatus.ACTIVE,
        )
    )
    return wax_id


def _save_history(storage, wax_id, *, text, author=AuthorKind.STUDENT, hours_ago=0):
    entry = HistoryEntry(
        id=new_id(),
        wax_id=wax_id,
        author_kind=author,
        content_kind="text",
        text=text,
        occurred_at=NOW - timedelta(hours=hours_ago),
        recorded_at=NOW - timedelta(hours=hours_ago),
    )
    storage.append_history_entry(entry)
    # Index the text so find_exact and find_by_meaning can retrieve it.
    indexer = Indexer(storage, TokenIndex(TEST_CIPHER), _Clock())
    indexer.index(wax_id, ObjectType.HISTORY, entry.id, text, entry.occurred_at)
    return entry


def _build_investigator(storage, cipher, decisions, final=None):
    indexer = Indexer(storage, TokenIndex(cipher), _Clock())
    evolution = EvolutionService(storage=storage, indexer=indexer)
    from waxprep.context.retrieval import Retriever

    retriever = Retriever(storage, TokenIndex(cipher))
    tools = build_default_tools(storage, retriever, None, evolution=evolution)
    model = MockContextModel(decision_script=list(decisions), final=final)
    return ContextInvestigator(model=model, tools=tools)


# ---------------------------------------------------------------------------
# Acceptance test 1: Quiz that started long ago (blueprint Part 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiz_evidence_retrieved_from_beyond_any_recent_window() -> None:
    """CI must be able to find the original quiz instruction and score
    evidence even when they are old. No fixed turn window limits what
    CI can investigate."""
    storage = InMemoryStorage()
    wax_id = _setup_student(storage)

    # Turn 1: quiz starts (many hours ago — well beyond any 6-turn window)
    _save_history(storage, wax_id, text="Please start a biology quiz.", hours_ago=48)
    # Turn 2: student answers
    _save_history(storage, wax_id, text="The cell.", hours_ago=47)
    # Turn 3: tutor says correct
    _save_history(
        storage,
        wax_id,
        text="Correct! Score is 1/1. Next question...",
        author=AuthorKind.TUTOR,
        hours_ago=47,
    )
    # Many turns later (turn 10+): student asks about score
    trigger = _save_history(
        storage, wax_id, text="What is my score so far?", hours_ago=0
    )

    # CI scripts: find the quiz + score evidence
    decisions = (
        ContextModelDecision(
            "find_exact",
            {"query": "score"},
            "find score mentions",
        ),
        ContextModelDecision("stop", {}, "found the score"),
    )
    final = ContextModelFinal(
        summary="Student started a biology quiz 48 hours ago. Score is 1/1.",
        selected_evidence_ids=(),
        uncertainty=(),
        stopped_reason="model_finalized",
    )

    investigator = _build_investigator(storage, TEST_CIPHER, decisions, final)
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=wax_id,
            query="What is my score so far?",
            now=NOW,
            trigger_entry_id=trigger.id,
        )
    )

    # CI must have found at least the score evidence
    assert len(package.evidence) >= 1
    assert any(
        "1/1" in e.content or "score" in e.content.lower() for e in package.evidence
    )


# ---------------------------------------------------------------------------
# Acceptance test 2: Natural follow-up (blueprint Part 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_natural_follow_up_finds_prior_explanation() -> None:
    """Student says "explain that again, but more simply." CI must find
    the prior explanation rather than leaving the Teacher to guess what
    "that" refers to."""
    storage = InMemoryStorage()
    wax_id = _setup_student(storage)

    _save_history(
        storage,
        wax_id,
        text="Photosynthesis is the process by which plants convert light energy into chemical energy.",
        author=AuthorKind.TUTOR,
        hours_ago=1,
    )
    trigger = _save_history(
        storage,
        wax_id,
        text="Can you explain that again, but more simply?",
        hours_ago=0,
    )

    decisions = (
        ContextModelDecision(
            "read_around",
            {"entry_id": str(trigger.id), "before": 4, "after": 0},
            "find what 'that' refers to",
        ),
        ContextModelDecision("stop", {}, "found the prior explanation"),
    )
    final = ContextModelFinal(
        summary="Student is asking for a simpler version of the photosynthesis explanation from 1 hour ago.",
        selected_evidence_ids=(),
        uncertainty=(),
        stopped_reason="model_finalized",
    )

    investigator = _build_investigator(storage, TEST_CIPHER, decisions, final)
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=wax_id,
            query="Can you explain that again, but more simply?",
            now=NOW,
            trigger_entry_id=trigger.id,
        )
    )

    # CI must have found the prior explanation
    assert len(package.evidence) >= 1
    assert any("photosynthesis" in e.content.lower() for e in package.evidence)


# ---------------------------------------------------------------------------
# Acceptance test 3: Returning student says hello (blueprint Part 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returning_student_hello_uses_notebook_knowledge() -> None:
    """A returning student says "Hi." CI can find relevant notebook
    knowledge to help the Teacher respond warmly without assuming
    the student wants to resume an old task."""
    storage = InMemoryStorage()
    wax_id = _setup_student(storage)

    history = _save_history(
        storage, wax_id, text="I'm studying biology for WAEC.", hours_ago=72
    )

    # Create a notebook entry
    indexer = Indexer(storage, TokenIndex(TEST_CIPHER), _Clock())
    evolution = EvolutionService(storage=storage, indexer=indexer)
    result = evolution.apply(
        Proposal(
            wax_id=wax_id,
            operation=Operation.ADD_NOTEBOOK_ENTRY.value,
            payload={"payload": {"subject": "biology", "exam": "WAEC"}},
            evidence=(EvidenceRef(ObjectType.HISTORY, history.id),),
            reason="student stated their subject and exam",
        ),
        now=NOW - timedelta(hours=72),
    )
    assert result.accepted

    trigger = _save_history(storage, wax_id, text="Hi", hours_ago=0)

    decisions = (
        ContextModelDecision(
            "find_by_meaning",
            {"query": "biology WAEC student"},
            "check notebook for stable facts",
        ),
        ContextModelDecision("stop", {}, "found notebook knowledge"),
    )
    final = ContextModelFinal(
        summary="Student is studying biology for WAEC. Greet warmly; don't assume they want to resume.",
        selected_evidence_ids=(),
        uncertainty=(),
        stopped_reason="model_finalized",
    )

    investigator = _build_investigator(storage, TEST_CIPHER, decisions, final)
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=wax_id,
            query="Hi",
            now=NOW,
            trigger_entry_id=trigger.id,
        )
    )

    # CI found relevant context (notebook or history)
    assert package.investigation_status.value in ("successful", "empty")
    # The summary should mention biology or WAEC
    assert "biology" in package.summary.lower() or "waec" in package.summary.lower()


# ---------------------------------------------------------------------------
# Acceptance test 4: Changed preference updates notebook (blueprint Part 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_preference_supersedes_notebook_entry() -> None:
    """Student previously said they prefer short explanations. Now they
    say they prefer detailed ones. CI should propose a supersession.
    The old entry is preserved; the new one is current."""
    storage = InMemoryStorage()
    wax_id = _setup_student(storage)

    old_history = _save_history(
        storage, wax_id, text="I prefer short explanations.", hours_ago=24
    )
    trigger = _save_history(
        storage,
        wax_id,
        text="Actually I prefer detailed explanations with examples.",
        hours_ago=0,
    )

    # Create old notebook entry
    indexer = Indexer(storage, TokenIndex(TEST_CIPHER), _Clock())
    evolution = EvolutionService(storage=storage, indexer=indexer)
    old_result = evolution.apply(
        Proposal(
            wax_id=wax_id,
            operation=Operation.ADD_NOTEBOOK_ENTRY.value,
            payload={"payload": {"preference": "short explanations"}},
            evidence=(EvidenceRef(ObjectType.HISTORY, old_history.id),),
            reason="student stated preference",
        ),
        now=NOW - timedelta(hours=24),
    )
    assert old_result.accepted
    assert old_result.created_notebook_entry_id is not None
    old_entry_id = old_result.created_notebook_entry_id

    # CI proposes supersession
    decisions = (
        ContextModelDecision(
            "find_by_meaning",
            {"query": "explanation preference"},
            "retrieve existing preference",
        ),
        ContextModelDecision(
            "propose_lifecycle_change",
            {
                "operation": Operation.SUPERSEDE_NOTEBOOK_ENTRY.value,
                "payload": {
                    "old_entry_id": str(old_entry_id),
                    "payload": {"preference": "detailed explanations with examples"},
                },
                "evidence_refs": [
                    {
                        "object_type": ObjectType.HISTORY.value,
                        "object_id": str(trigger.id),
                    }
                ],
                "reason": "student corrected their preference",
            },
            "supersede old preference",
        ),
        ContextModelDecision("stop", {}, "supersession proposed"),
    )
    final = ContextModelFinal(
        summary="Student corrected their preference from short to detailed explanations.",
        selected_evidence_ids=(),
        uncertainty=(),
        stopped_reason="model_finalized",
    )

    investigator = _build_investigator(storage, TEST_CIPHER, decisions, final)
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=wax_id,
            query="Actually I prefer detailed explanations with examples.",
            now=NOW,
            trigger_entry_id=trigger.id,
        )
    )

    # The supersession was accepted
    assert any(
        "proposal_accepted:supersede_notebook_entry" in step
        for step in package.investigated
    ), f"Expected proposal_accepted in audit trail: {package.investigated}"

    # The old entry is SUPERSEDED, the new one is ACTIVE
    notebook = storage.get_notebook(wax_id)
    assert notebook is not None
    entries = storage.list_notebook_entries(
        wax_id, notebook.id, include_superseded=True
    )
    assert len(entries) == 2  # old + new
    statuses = {e.status.value for e in entries}
    assert "superseded" in statuses
    assert "active" in statuses


# ---------------------------------------------------------------------------
# Acceptance test 5: No durable learning (blueprint Part 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_durable_learning_notebook_unchanged() -> None:
    """Student says "I'm tired today." The post-turn review runs but
    the notebook remains unchanged — not every message justifies a
    permanent fact."""
    storage = InMemoryStorage()
    wax_id = _setup_student(storage)
    trigger = _save_history(storage, wax_id, text="I'm tired today.", hours_ago=0)

    decisions = (ContextModelDecision("stop", {}, "no durable knowledge to save"),)
    final = ContextModelFinal(
        summary="No durable knowledge to save from this message.",
        selected_evidence_ids=(),
        uncertainty=(),
        stopped_reason="model_finalized",
    )

    investigator = _build_investigator(storage, TEST_CIPHER, decisions, final)
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=wax_id,
            query="I'm tired today.",
            now=NOW,
            trigger_entry_id=trigger.id,
        )
    )

    # No proposal was made
    assert not any("proposal_" in step for step in package.investigated)
    # Notebook is empty
    notebook = storage.get_notebook(wax_id)
    if notebook is not None:
        entries = storage.list_notebook_entries(wax_id, notebook.id)
        assert len(entries) == 0


# ---------------------------------------------------------------------------
# Acceptance test 6: Missing evidence — Teacher asks clarification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_evidence_surfaces_uncertainty() -> None:
    """Student asks about a quiz that doesn't exist in history. CI finds
    nothing relevant and surfaces uncertainty. The Teacher should ask
    a natural clarifying question rather than invent a score."""
    storage = InMemoryStorage()
    wax_id = _setup_student(storage)
    trigger = _save_history(
        storage, wax_id, text="What was my score on the chemistry test?", hours_ago=0
    )

    decisions = (
        ContextModelDecision(
            "browse_time_window",
            {"limit": 5, "newest_first": True},
            "check recent history",
        ),
        ContextModelDecision("stop", {}, "no chemistry test found"),
    )
    final = ContextModelFinal(
        summary="No prior history mentions a chemistry test.",
        selected_evidence_ids=(),
        uncertainty=("no evidence of a chemistry test was found",),
        stopped_reason="model_finalized",
    )

    investigator = _build_investigator(storage, TEST_CIPHER, decisions, final)
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=wax_id,
            query="What was my score on the chemistry test?",
            now=NOW,
            trigger_entry_id=trigger.id,
        )
    )

    # CI honestly reports it found nothing
    assert package.uncertainty, "CI must surface uncertainty when evidence is missing"
    assert any(
        "chemistry" in u.lower() or "no evidence" in u.lower()
        for u in package.uncertainty
    )
