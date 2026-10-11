"""Derived understanding must always point back to real evidence."""

from __future__ import annotations

from uuid import uuid4

from support import World, add_student, make_world
from waxprep.context.evolution import NEEDS_CONFIRMATION
from waxprep.context.proposals import EvidenceRef, Operation, Proposal
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, NotebookEntryStatus, ObjectType
from waxprep.knowledge.models import KnowledgeNodeStatus


def _entry(world: World, wax: WaxId, text: str, author: AuthorKind) -> EvidenceRef:
    entry = world.services.history.record(wax, author, text)
    return EvidenceRef(ObjectType.HISTORY, entry.id, quote=text)


def _note(wax: WaxId, payload: dict[str, object], *refs: EvidenceRef) -> Proposal:
    return Proposal(wax, Operation.ADD_NOTEBOOK_ENTRY.value, {"payload": payload}, refs)


def test_notebook_entry_links_back_to_student_evidence() -> None:
    world = make_world()
    wax = add_student(world)
    ref = _entry(world, wax, "I study maths after school", AuthorKind.STUDENT)
    result = world.services.evolution.apply(
        _note(wax, {"observation": "studies maths after school"}, ref),
        now=world.clock.now(),
    )
    assert result.accepted and result.created_notebook_entry_id
    links = world.storage.list_provenance(
        wax, ObjectType.NOTEBOOK, result.created_notebook_entry_id
    )
    assert [(l.source_id, l.source_author_kind) for l in links] == [
        (ref.object_id, AuthorKind.STUDENT)
    ]
    assert links[0].quote == "I study maths after school"
    entry = world.storage.get_notebook_entry(wax, result.created_notebook_entry_id)
    assert entry is not None and NEEDS_CONFIRMATION not in entry.payload


def test_what_the_tutor_said_never_becomes_a_student_fact() -> None:
    world = make_world()
    wax = add_student(world)
    ref = _entry(world, wax, "You told me you are preparing for WAEC", AuthorKind.TUTOR)
    result = world.services.evolution.apply(
        _note(wax, {"observation": "preparing for WAEC"}, ref), now=world.clock.now()
    )
    assert result.created_notebook_entry_id
    entry = world.storage.get_notebook_entry(wax, result.created_notebook_entry_id)
    assert entry is not None and entry.payload[NEEDS_CONFIRMATION] is True


def test_changes_without_real_evidence_are_rejected() -> None:
    world = make_world()
    wax = add_student(world)
    now = world.clock.now()
    none = world.services.evolution.apply(_note(wax, {"a": 1}), now=now)
    fake = world.services.evolution.apply(
        _note(wax, {"a": 1}, EvidenceRef(ObjectType.HISTORY, uuid4())), now=now
    )
    assert not none.accepted and not fake.accepted
    assert "unknown evidence" in fake.reason


def test_evidence_from_another_student_is_rejected() -> None:
    world = make_world()
    a, b = add_student(world), add_student(world)
    theirs = _entry(world, b, "b's private message", AuthorKind.STUDENT)
    result = world.services.evolution.apply(
        _note(a, {"x": 1}, theirs), now=world.clock.now()
    )
    assert not result.accepted


def test_replacing_understanding_keeps_old_entry_and_all_history() -> None:
    world = make_world()
    wax = add_student(world)
    first_ref = _entry(world, wax, "I study maths at night", AuthorKind.STUDENT)
    first = world.services.evolution.apply(
        _note(wax, {"observation": "studies at night"}, first_ref),
        now=world.clock.now(),
    )
    world.clock.advance(86400 * 40)
    second_ref = _entry(
        world, wax, "Actually I now study in the morning", AuthorKind.STUDENT
    )
    second = world.services.evolution.apply(
        Proposal(
            wax,
            Operation.SUPERSEDE_NOTEBOOK_ENTRY.value,
            {
                "old_entry_id": str(first.created_notebook_entry_id),
                "payload": {"observation": "studies in the morning"},
            },
            (second_ref,),
        ),
        now=world.clock.now(),
    )
    assert second.accepted
    assert first.created_notebook_entry_id is not None
    old = world.storage.get_notebook_entry(wax, first.created_notebook_entry_id)
    assert old is not None
    assert old.status is NotebookEntryStatus.SUPERSEDED
    assert old.superseded_by_id == second.created_notebook_entry_id
    notebook = world.storage.get_notebook(wax)
    assert notebook is not None
    active = world.storage.list_notebook_entries(wax, notebook.id)
    assert [e.id for e in active] == [second.created_notebook_entry_id]
    assert len(world.storage.list_history(wax)) == 2  # evidence untouched


def test_knowledge_supersession_preserves_both_versions() -> None:
    world = make_world()
    wax = add_student(world)
    ref = _entry(world, wax, "I want University X", AuthorKind.STUDENT)
    now = world.clock.now()
    first = world.services.evolution.apply(
        Proposal(
            wax,
            Operation.CREATE_NODE.value,
            {"node_type": "plan", "label": "University X"},
            (ref,),
        ),
        now=now,
    )
    later = _entry(world, wax, "I'm going to University Y now", AuthorKind.STUDENT)
    second = world.services.evolution.apply(
        Proposal(
            wax,
            Operation.SUPERSEDE_NODE.value,
            {
                "old_node_id": str(first.created_node_id),
                "node_type": "plan",
                "label": "University Y",
            },
            (later,),
        ),
        now=now,
    )
    assert first.created_node_id and second.created_node_id
    old = world.storage.get_node(wax, first.created_node_id)
    new = world.storage.get_node(wax, second.created_node_id)
    assert old is not None and old.status is KnowledgeNodeStatus.SUPERSEDED
    assert new is not None and new.status is KnowledgeNodeStatus.ACTIVE
    assert old.label == "University X"
    current = world.services.retriever.check_currency(
        wax, first.created_node_id, ObjectType.KNOWLEDGE_NODE
    )
    assert any("University Y" in e.content for e in current)


def test_edges_cannot_cross_students() -> None:
    world = make_world()
    a, b = add_student(world), add_student(world)
    now = world.clock.now()
    na = world.services.evolution.apply(
        Proposal(
            a,
            Operation.CREATE_NODE.value,
            {"node_type": "x", "label": "A"},
            (_entry(world, a, "a says", AuthorKind.STUDENT),),
        ),
        now=now,
    )
    nb = world.services.evolution.apply(
        Proposal(
            b,
            Operation.CREATE_NODE.value,
            {"node_type": "x", "label": "B"},
            (_entry(world, b, "b says", AuthorKind.STUDENT),),
        ),
        now=now,
    )
    cross = world.services.evolution.apply(
        Proposal(
            a,
            Operation.CREATE_EDGE.value,
            {
                "source_node_id": str(na.created_node_id),
                "target_node_id": str(nb.created_node_id),
                "relation": "knows",
            },
            (_entry(world, a, "link them", AuthorKind.STUDENT),),
        ),
        now=now,
    )
    assert not cross.accepted and "unknown target" in cross.reason


def test_unknown_operations_are_refused() -> None:
    world = make_world()
    wax = add_student(world)
    result = world.services.evolution.apply(
        Proposal(
            wax, "drop_everything", {}, (_entry(world, wax, "x", AuthorKind.STUDENT),)
        ),
        now=world.clock.now(),
    )
    assert not result.accepted
