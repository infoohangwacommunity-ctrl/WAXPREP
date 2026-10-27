"""Context Intelligence discovers judge assessments through its normal tools.

The judge never talks to CI directly: it writes a notebook entry via
the existing proposal path, the entry is token-indexed like any other,
and CI's ordinary find_exact tool surfaces it as NOTEBOOK evidence.
This is the read side of the feedback loop.
"""

from __future__ import annotations

from support import TEST_CIPHER, World, add_student, make_world
from test_quality_judge import FakeJudgeModel, add_pair, make_judge
from waxprep.context.evidence import notebook_evidence
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision
from waxprep.context.models import (
    ContextPackage,
    EvidenceType,
    InvestigationRequest,
)
from waxprep.context.retrieval import Retriever
from waxprep.context.tool_impls import build_default_tools
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import NotebookEntry, new_id
from waxprep.index.tokens import TokenIndex
from waxprep.judge.models import JudgeRequest
from waxprep.judge.service import NOTEBOOK_KIND

ASSESSMENT_QUERY = "worked examples"


def make_investigator(world: World) -> ContextInvestigator:
    """An investigator wired exactly like the production composition."""
    retriever = Retriever(world.storage, TokenIndex(TEST_CIPHER))
    return ContextInvestigator(
        model=world.context_model,
        tools=build_default_tools(
            world.storage, retriever, None, evolution=world.services.evolution
        ),
    )


async def investigate(world: World, wax_id: WaxId, query: str) -> ContextPackage:
    """Run one investigation; reset the scripted model between runs."""
    world.context_model.calls = 0
    investigator = make_investigator(world)
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=wax_id,
            query=query,
            now=world.clock.now(),
            trigger_entry_id=None,
        )
    )
    return package


async def test_ci_finds_judge_assessment_with_ordinary_tools() -> None:
    """A scripted find_exact surfaces the judge's notebook entry."""
    world = make_world(
        context_model=MockContextModel(
            decision_script=[
                ContextModelDecision(
                    "find_exact",
                    {"query": ASSESSMENT_QUERY},
                    "find the tutor-quality assessment",
                ),
                ContextModelDecision("stop", {}, "enough evidence"),
            ]
        )
    )
    wax_id = add_student(world)

    # No assessment yet: the loop starts empty.
    package = await investigate(world, wax_id, "Help me with fractions")
    assert not [e for e in package.evidence if e.evidence_type is EvidenceType.NOTEBOOK]

    # Run one judge cycle so the assessment lands in the notebook.
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)
    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    outcome = await judge.evaluate_recent(wax_id)
    assert outcome.status == "written"
    assert outcome.notebook_entry_id is not None

    package = await investigate(world, wax_id, "Help me with fractions")
    notebook_items = [
        e for e in package.evidence if e.evidence_type is EvidenceType.NOTEBOOK
    ]
    assert notebook_items, "CI must be able to find the judge assessment"
    judge_items = [
        e
        for e in notebook_items
        if NOTEBOOK_KIND in e.content and ASSESSMENT_QUERY in e.content
    ]
    assert judge_items, "the found notebook evidence must be the assessment"
    # The judge's guidance — not a bare score — travelled intact.
    assert any("connect new topics" in e.content for e in judge_items)
    # Scores travel as context, and the entry is dated like all evidence.
    assert all(e.created_at is not None for e in judge_items)


async def test_superseded_assessment_stays_single_after_two_cycles() -> None:
    """After a second judge cycle, discovery still finds exactly one
    active assessment — the superseded one is history, not noise."""
    world = make_world(
        context_model=MockContextModel(
            decision_script=[
                ContextModelDecision(
                    "find_exact",
                    {"query": ASSESSMENT_QUERY},
                    "find the assessment",
                ),
                ContextModelDecision("stop", {}, "done"),
            ]
        )
    )
    wax_id = add_student(world)
    model = FakeJudgeModel()
    judge = make_judge(world, model, every_n_turns=2)
    add_pair(world, wax_id, 1)
    add_pair(world, wax_id, 2)
    first = await judge.evaluate_recent(wax_id)
    assert first.status == "written"

    model.response["overall"] = 5
    add_pair(world, wax_id, 3)
    add_pair(world, wax_id, 4)
    second = await judge.evaluate_recent(wax_id)
    assert second.status == "written"

    package = await investigate(world, wax_id, "What next?")
    assessment_items = [e for e in package.evidence if NOTEBOOK_KIND in e.content]
    assert assessment_items, "the assessment must be discoverable"
    # Exactly one *active* assessment is in play. If the superseded
    # entry still lingers in the token index, it must arrive openly
    # marked as superseded — never as a second live assessment.
    active = [e for e in assessment_items if e.decision.value != "superseded"]
    assert len(active) == 1
    assert "improving" in active[0].content

    notebook = world.storage.get_notebook(wax_id)
    assert notebook is not None
    stored_active = [
        e
        for e in world.storage.list_notebook_entries(wax_id, notebook.id)
        if e.payload.get("kind") == NOTEBOOK_KIND
    ]
    assert len(stored_active) == 1


def test_notebook_evidence_renders_assessment_content() -> None:
    """The evidence renderer carries the assessment payload verbatim."""
    world = make_world()
    wax_id = add_student(world)
    now = world.clock.now()
    notebook = world.storage.get_or_create_notebook(wax_id, now)
    entry = world.storage.add_notebook_entry(
        NotebookEntry(
            id=new_id(),
            wax_id=wax_id,
            notebook_id=notebook.id,
            schema_version=1,
            payload={
                "kind": NOTEBOOK_KIND,
                "rating": 4,
                "tutor_guidance": "Keep using worked examples for this student.",
            },
            created_at=now,
            updated_at=now,
        )
    )
    evidence = notebook_evidence(entry, "test")
    assert evidence.evidence_type is EvidenceType.NOTEBOOK
    assert "worked examples" in evidence.content
    assert NOTEBOOK_KIND in evidence.content


def test_judge_request_carries_window_facts_only() -> None:
    """The judge model request carries window facts, never topic labels."""
    request = JudgeRequest(
        wax_id=new_wax_id(),
        window_text="student: hi\ntutor: hello",
        window_first_id=new_id(),
        window_last_id=new_id(),
        window_turns=1,
        previous_payload=None,
    )
    assert request.window_turns == 1
    assert request.previous_payload is None
    assert "student: hi" in request.window_text
