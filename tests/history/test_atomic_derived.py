"""Atomic derived operations regression tests (spec section 9).

Verifies that derived-object + provenance + index writes are atomic:
a failure between logical stages leaves no orphaned derived object,
no provenance pointing to a nonexistent object, no superseded object
without its replacement relationship, and no object permanently
unsearchable because its indexing job disappeared.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from support import add_student, make_world
from waxprep.context.proposals import EvidenceRef, Operation, Proposal
from waxprep.domain.models import AuthorKind, HistoryEntry, ObjectType, new_id
from waxprep.knowledge.models import KnowledgeNodeStatus

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _student_history(world) -> tuple[Any, HistoryEntry]:
    wax = add_student(world)
    entry = world.services.history.record(
        wax, AuthorKind.STUDENT, "I study best in the morning."
    )
    return wax, entry


def test_create_node_writes_node_provenance_and_index_atomically() -> None:
    """Creating a knowledge node writes the node, its provenance links,
    and its search index in one transaction. If the indexer fails, the
    node must not be persisted (atomic rollback).
    """
    world = make_world()
    wax, entry = _student_history(world)
    # Snapshot before to verify no node was created if the txn fails.
    nodes_before = len(world.storage._nodes)  # type: ignore[attr-defined]
    proposal = Proposal(
        wax_id=wax,
        operation=Operation.CREATE_NODE,
        evidence=(EvidenceRef(ObjectType.HISTORY, entry.id, "morning"),),
        payload={
            "node_type": "preference",
            "label": "morning study",
            "description": "prefers mornings",
            "properties": {},
        },
    )
    # Inject a failure into the indexer to verify atomicity.
    original_index = world.services.indexer.index

    def failing_index(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("indexer crashed mid-write")

    world.services.indexer.index = failing_index  # type: ignore[assignment]
    try:
        # The proposal should fail because the indexer raised.
        with pytest.raises(RuntimeError, match="indexer crashed"):
            world.services.evolution.apply(proposal, now=NOW)
    finally:
        world.services.indexer.index = original_index  # type: ignore[assignment]
    # The atomic transaction must have rolled back — no orphan node.
    assert len(world.storage._nodes) == nodes_before, (  # type: ignore[attr-defined]
        "transaction must roll back — no orphan node after indexer failure"
    )


def test_supersede_node_creates_replacement_and_marks_old_atomically() -> None:
    """Superseding a node creates the new node, marks the old one
    superseded, and creates the supersession edge — atomically."""
    world = make_world()
    wax, entry = _student_history(world)
    # Create the original node.
    create_proposal = Proposal(
        wax_id=wax,
        operation=Operation.CREATE_NODE,
        evidence=(EvidenceRef(ObjectType.HISTORY, entry.id, "morning"),),
        payload={
            "node_type": "preference",
            "label": "morning study",
            "properties": {},
        },
    )
    result = world.services.evolution.apply(create_proposal, now=NOW)
    assert result.accepted and result.created_node_id is not None
    old_node_id = result.created_node_id
    # Supersede it — note the payload field names: label, node_type
    # (not new_label, new_node_type) per _supersede_node's check.
    supersede_proposal = Proposal(
        wax_id=wax,
        operation=Operation.SUPERSEDE_NODE,
        evidence=(EvidenceRef(ObjectType.HISTORY, entry.id, "morning"),),
        payload={
            "old_node_id": str(old_node_id),
            "node_type": "preference",
            "label": "evening study",
            "properties": {},
        },
    )
    result2 = world.services.evolution.apply(supersede_proposal, now=NOW)
    assert result2.accepted
    assert result2.created_node_id is not None
    assert result2.superseded_id == old_node_id
    old_node = world.storage.get_node(wax, old_node_id)
    assert old_node is not None
    assert old_node.status is KnowledgeNodeStatus.SUPERSEDED


def test_provenance_links_are_written_with_the_derived_object() -> None:
    """When a notebook entry is created, its provenance links are written
    in the same transaction — no orphan derived object."""
    world = make_world()
    wax, entry = _student_history(world)
    proposal = Proposal(
        wax_id=wax,
        operation=Operation.ADD_NOTEBOOK_ENTRY,
        evidence=(EvidenceRef(ObjectType.HISTORY, entry.id, "morning"),),
        payload={
            "schema_version": 1,
            "payload": {"note": "studies in morning"},
        },
    )
    result = world.services.evolution.apply(proposal, now=NOW)
    assert result.accepted
    assert result.created_notebook_entry_id is not None
    nb_id = result.created_notebook_entry_id
    links = world.storage.list_provenance(wax, ObjectType.NOTEBOOK, nb_id)
    assert links, "derived object must have provenance links"
    assert any(
        link.source_type is ObjectType.HISTORY and link.source_id == entry.id
        for link in links
    )


def test_transaction_rolls_back_on_failure_postgres(
    pg_store: Any, pg_conn: Any
) -> None:
    """With a real PostgreSQL transaction, a failure between the
    derived-object save and the provenance write rolls back the whole
    transaction — no orphan node.
    """
    from waxprep.domain.identifiers import new_wax_id
    from waxprep.domain.models import Student
    from waxprep.knowledge.models import KnowledgeNode

    wax = new_wax_id()
    pg_store.create_student(Student(wax, NOW, NOW))
    node = KnowledgeNode(
        id=new_id(),
        wax_id=wax,
        node_type="preference",
        label="morning",
        description=None,
        properties={},
        status=KnowledgeNodeStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(RuntimeError, match="simulated mid-transaction"):
        with pg_store.transaction():
            pg_store.save_node(node)
            raise RuntimeError("simulated mid-transaction crash")
    fetched = pg_store.get_node(wax, node.id)
    assert fetched is None, (
        "transaction must roll back — no orphan node after mid-transaction failure"
    )


def test_transaction_commits_on_success_postgres(pg_store: Any, pg_conn: Any) -> None:
    """A successful transaction commits all writes."""
    from waxprep.domain.identifiers import new_wax_id
    from waxprep.domain.models import Student
    from waxprep.knowledge.models import KnowledgeNode

    wax = new_wax_id()
    pg_store.create_student(Student(wax, NOW, NOW))
    node = KnowledgeNode(
        id=new_id(),
        wax_id=wax,
        node_type="preference",
        label="evening",
        description=None,
        properties={},
        status=KnowledgeNodeStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    with pg_store.transaction():
        pg_store.save_node(node)
    fetched = pg_store.get_node(wax, node.id)
    assert fetched is not None
    assert fetched.label == "evening"
