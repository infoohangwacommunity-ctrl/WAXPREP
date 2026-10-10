"""Offline CI evaluation: scripted model + real retrieval (no keyword routing)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision, ContextModelFinal
from waxprep.context.models import InvestigationRequest
from waxprep.context.retrieval import HybridRetriever
from waxprep.context.tool_impls import build_default_tools
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Conversation,
    Message,
    MessageContentType,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.knowledge.models import KnowledgeEdge, KnowledgeNode, KnowledgeNodeStatus
from waxprep.knowledge.store import KnowledgeStore
from waxprep.storage.memory import InMemoryStorage


def _seed_student(store: InMemoryStorage, now: datetime) -> Student:
    student = Student(
        wax_id=new_wax_id(),
        created_at=now,
        updated_at=now,
        status=StudentStatus.ACTIVE,
    )
    store.create_student(student)
    return student


def _add_message(
    store: InMemoryStorage,
    student: Student,
    text: str,
    now: datetime,
    *,
    conversation_id: UUID | None = None,
) -> tuple[Message, UUID]:
    conv_id = conversation_id or new_id()
    if conversation_id is None:
        store.create_conversation(
            Conversation(
                id=conv_id,
                wax_id=student.wax_id,
                created_at=now,
                updated_at=now,
            )
        )
    msg = Message(
        id=new_id(),
        wax_id=student.wax_id,
        conversation_id=conv_id,
        role="student",
        content_type=MessageContentType.TEXT,
        text_body=text,
        created_at=now,
    )
    store.create_message(msg)
    return msg, conv_id


@pytest.mark.asyncio
async def test_scenario_distant_history_retrieved_by_script() -> None:
    """Historical evidence is found without requiring current-message keywords."""
    store = InMemoryStorage()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    student = _seed_student(store, now)
    old = now - timedelta(days=30)
    gold, conv_id = _add_message(
        store,
        student,
        "We stopped after question 3 on the unfinished activity.",
        old,
    )
    _add_message(
        store,
        student,
        "unrelated chat about weather",
        now,
        conversation_id=conv_id,
    )

    retriever = HybridRetriever(store)
    tools = build_default_tools(retriever)
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                action="search_conversation",
                arguments={"query": "unfinished activity", "limit": 5},
                reason="Investigate prior unfinished work.",
            ),
            ContextModelDecision(
                action="stop",
                arguments={},
                reason="Found candidate history.",
            ),
        ],
        final=ContextModelFinal(
            summary="Student left an unfinished activity at question 3.",
            selected_evidence_ids=(str(gold.id),),
            uncertainty=(),
            stopped_reason="enough_evidence",
        ),
    )
    inv = ContextInvestigator(model=model, tools=tools)  # type: ignore[arg-type]
    package = await inv.investigate(
        InvestigationRequest(
            wax_id=student.wax_id,
            conversation_id=conv_id,
            query="Can we continue that thing we stopped?",
        )
    )
    assert any(e.id == gold.id for e in package.evidence)
    assert package.summary


@pytest.mark.asyncio
async def test_scenario_no_matching_evidence_does_not_invent() -> None:
    store = InMemoryStorage()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    student = _seed_student(store, now)
    conv = Conversation(
        id=new_id(), wax_id=student.wax_id, created_at=now, updated_at=now
    )
    store.create_conversation(conv)
    retriever = HybridRetriever(store)
    tools = build_default_tools(retriever)
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                action="search_conversation",
                arguments={"query": "red thing", "limit": 5},
                reason="Look for prior reference.",
            ),
            ContextModelDecision(
                action="stop",
                arguments={},
                reason="Nothing found.",
            ),
        ],
        final=ContextModelFinal(
            summary="No matching prior evidence was found.",
            selected_evidence_ids=(),
            uncertainty=("no_matching_evidence",),
            stopped_reason="no_evidence",
        ),
    )
    inv = ContextInvestigator(model=model, tools=tools)  # type: ignore[arg-type]
    package = await inv.investigate(
        InvestigationRequest(
            wax_id=student.wax_id,
            conversation_id=conv.id,
            query="Can we pick up that red thing from before?",
        )
    )
    assert package.evidence == ()
    assert "no_matching_evidence" in package.uncertainty or package.stopped_reason


@pytest.mark.asyncio
async def test_scenario_relationship_traversal() -> None:
    store = InMemoryStorage()
    knowledge = KnowledgeStore()
    now = datetime(2026, 6, 1, tzinfo=UTC)
    student = _seed_student(store, now)
    person = KnowledgeNode(
        id=new_id(),
        wax_id=student.wax_id,
        node_type="person",
        label="Mr A",
        description="Mentioned by student",
        properties={},
        status=KnowledgeNodeStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    thing = KnowledgeNode(
        id=new_id(),
        wax_id=student.wax_id,
        node_type="object",
        label="item from Mr A",
        description="Something given to the student",
        properties={},
        status=KnowledgeNodeStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    knowledge.put_node(person)
    knowledge.put_node(thing)
    knowledge.put_edge(
        KnowledgeEdge(
            id=new_id(),
            wax_id=student.wax_id,
            source_node_id=person.id,
            target_node_id=thing.id,
            relation="gave",
            properties={},
            valid_from=now,
            valid_until=None,
            created_at=now,
        )
    )
    retriever = HybridRetriever(store, knowledge)
    tools = build_default_tools(retriever)
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                action="follow_relationships",
                arguments={"node_id": str(person.id), "direction": "outgoing"},
                reason="Follow associations from known person node.",
            ),
            ContextModelDecision(
                action="stop",
                arguments={},
                reason="Relationship inspected.",
            ),
        ]
    )
    inv = ContextInvestigator(model=model, tools=tools)  # type: ignore[arg-type]
    package = await inv.investigate(
        InvestigationRequest(
            wax_id=student.wax_id,
            conversation_id=uuid4(),
            query="What was that thing he gave me?",
        )
    )
    assert any(e.evidence_type.value == "knowledge_edge" for e in package.evidence)


@pytest.mark.asyncio
async def test_isolation_same_content_different_students() -> None:
    store = InMemoryStorage()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = _seed_student(store, now)
    b = _seed_student(store, now)
    secret_a, _ = _add_message(store, a, "private alpha secret phrase", now)
    _add_message(store, b, "private beta secret phrase", now)
    retriever = HybridRetriever(store)
    results = retriever.search_conversation(a.wax_id, "secret phrase", limit=10)
    assert all(e.wax_id == a.wax_id for e in results)
    assert any(e.id == secret_a.id for e in results)
    assert all("beta" not in e.content for e in results)
