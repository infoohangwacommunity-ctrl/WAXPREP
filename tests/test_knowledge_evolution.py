"""Context Intelligence write/evolution: propose → validate → persist → history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from waxprep.context.evolution import KnowledgeEvolutionService
from waxprep.context.history import TemporalFact, choose_current_fact
from waxprep.context.post_interaction import (
    KnowledgeOperation,
    KnowledgeProposal,
    KnowledgeProposalValidator,
)
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import Student, StudentStatus
from waxprep.knowledge.models import KnowledgeNodeStatus
from waxprep.knowledge.store import KnowledgeStore
from waxprep.storage.memory import InMemoryStorage


def test_rejects_unknown_operation() -> None:
    v = KnowledgeProposalValidator()
    d = v.validate(
        KnowledgeProposal(
            wax_id=new_wax_id(),
            source_conversation_id=uuid4(),
            operation="hack_the_db",
            payload={},
            evidence_ids=(),
        )
    )
    assert d.accepted is False


def test_rejects_create_node_without_label() -> None:
    v = KnowledgeProposalValidator()
    d = v.validate(
        KnowledgeProposal(
            wax_id=new_wax_id(),
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.CREATE_NODE.value,
            payload={"node_type": "concept"},
            evidence_ids=(uuid4(),),
        )
    )
    assert d.accepted is False


def test_create_node_and_edge_persist() -> None:
    store = KnowledgeStore()
    evo = KnowledgeEvolutionService(knowledge=store)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    wax = new_wax_id()
    conv = uuid4()
    a = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=conv,
            operation=KnowledgeOperation.CREATE_NODE.value,
            payload={
                "node_type": "person",
                "label": "Mr A",
                "description": "mentioned",
            },
            evidence_ids=(uuid4(),),
            reason="CI proposed a person node",
        ),
        now=now,
    )
    b = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=conv,
            operation=KnowledgeOperation.CREATE_NODE.value,
            payload={"node_type": "object", "label": "item from Mr A"},
            evidence_ids=(uuid4(),),
        ),
        now=now,
    )
    assert a.accepted and b.accepted
    edge = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=conv,
            operation=KnowledgeOperation.CREATE_EDGE.value,
            payload={
                "source_node_id": str(a.created_node_id),
                "target_node_id": str(b.created_node_id),
                "relation": "gave",
            },
            evidence_ids=(uuid4(),),
            reason="CI proposed a relationship",
        ),
        now=now,
    )
    assert edge.accepted
    assert edge.created_edge_id is not None
    edges = store.follow(wax, a.created_node_id, direction="outgoing")  # type: ignore[arg-type]
    assert len(edges) == 1
    assert edges[0].relation == "gave"


def test_supersede_preserves_history() -> None:
    store = KnowledgeStore()
    evo = KnowledgeEvolutionService(knowledge=store)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    wax = new_wax_id()
    first = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.CREATE_NODE.value,
            payload={"node_type": "plan", "label": "University X"},
            evidence_ids=(uuid4(),),
        ),
        now=now,
    )
    later = now + timedelta(days=400)
    second = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.SUPERSEDE_NODE.value,
            payload={
                "old_node_id": str(first.created_node_id),
                "node_type": "plan",
                "label": "University Y",
            },
            evidence_ids=(uuid4(),),
            reason="Student later attends Y",
        ),
        now=later,
    )
    assert second.accepted
    old = store.get_node(wax, first.created_node_id)  # type: ignore[arg-type]
    new = store.get_node(wax, second.created_node_id)  # type: ignore[arg-type]
    assert old is not None and old.status is KnowledgeNodeStatus.SUPERSEDED
    assert new is not None and new.status is KnowledgeNodeStatus.ACTIVE
    assert old.label == "University X"
    assert new.label == "University Y"
    # Temporal helper: both remain for historical questions
    facts = [
        TemporalFact(
            node_id=str(old.id),
            valid_from=old.created_at,
            valid_until=later,
            status=old.status.value,
        ),
        TemporalFact(
            node_id=str(new.id),
            valid_from=later,
            valid_until=None,
            status=new.status.value,
        ),
    ]
    current = choose_current_fact(facts, at=later + timedelta(days=1))
    assert len(current) == 1
    assert current[0].node_id == str(new.id)


def test_edge_cannot_cross_students() -> None:
    store = KnowledgeStore()
    evo = KnowledgeEvolutionService(knowledge=store)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = new_wax_id()
    b = new_wax_id()
    na = evo.apply(
        KnowledgeProposal(
            wax_id=a,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.CREATE_NODE.value,
            payload={"node_type": "x", "label": "A only"},
            evidence_ids=(uuid4(),),
        ),
        now=now,
    )
    nb = evo.apply(
        KnowledgeProposal(
            wax_id=b,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.CREATE_NODE.value,
            payload={"node_type": "x", "label": "B only"},
            evidence_ids=(uuid4(),),
        ),
        now=now,
    )
    cross = evo.apply(
        KnowledgeProposal(
            wax_id=a,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.CREATE_EDGE.value,
            payload={
                "source_node_id": str(na.created_node_id),
                "target_node_id": str(nb.created_node_id),
                "relation": "knows",
            },
            evidence_ids=(uuid4(),),
        ),
        now=now,
    )
    assert cross.accepted is False
    assert "unknown target" in cross.reason


def test_attach_evidence_and_notebook_entry() -> None:
    knowledge = KnowledgeStore()
    storage = InMemoryStorage()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    wax = new_wax_id()
    storage.create_student(
        Student(wax_id=wax, created_at=now, updated_at=now, status=StudentStatus.ACTIVE)
    )
    evo = KnowledgeEvolutionService(knowledge=knowledge, storage=storage)
    node = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.CREATE_NODE.value,
            payload={"node_type": "concept", "label": "open world idea"},
            evidence_ids=(uuid4(),),
        ),
        now=now,
    )
    ev = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.ATTACH_EVIDENCE.value,
            payload={
                "node_id": str(node.created_node_id),
                "source_type": "conversation",
                "source_id": str(uuid4()),
                "quote": "student said something supporting this",
            },
            evidence_ids=(uuid4(),),
        ),
        now=now,
    )
    assert ev.accepted
    records = knowledge.evidence_for_node(wax, node.created_node_id)  # type: ignore[arg-type]
    assert len(records) == 1
    note = evo.apply(
        KnowledgeProposal(
            wax_id=wax,
            source_conversation_id=uuid4(),
            operation=KnowledgeOperation.ADD_NOTEBOOK_ENTRY.value,
            payload={"payload": {"summary": "retained understanding"}},
            evidence_ids=(),
        ),
        now=now,
    )
    assert note.accepted
    nb = storage.get_notebook(wax)
    assert nb is not None
    entries = storage.list_notebook_entries(wax, nb.id)
    assert len(entries) == 1
