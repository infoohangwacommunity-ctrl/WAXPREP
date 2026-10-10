"""Student isolation for CI retrieval (in-memory always; postgres opt-in)."""

from __future__ import annotations

from datetime import UTC, datetime

from waxprep.context.retrieval import HybridRetriever
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Conversation,
    Message,
    MessageContentType,
    NotebookEntry,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.knowledge.models import KnowledgeNode, KnowledgeNodeStatus
from waxprep.knowledge.store import KnowledgeStore
from waxprep.storage.memory import InMemoryStorage


def test_conversation_search_never_crosses_students() -> None:
    store = InMemoryStorage()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    b = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    store.create_student(a)
    store.create_student(b)
    for student, text in ((a, "alpha only"), (b, "beta only")):
        conv = Conversation(
            id=new_id(), wax_id=student.wax_id, created_at=now, updated_at=now
        )
        store.create_conversation(conv)
        store.create_message(
            Message(
                id=new_id(),
                wax_id=student.wax_id,
                conversation_id=conv.id,
                role="student",
                content_type=MessageContentType.TEXT,
                text_body=text,
                created_at=now,
            )
        )
    retriever = HybridRetriever(store)
    a_hits = retriever.search_conversation(a.wax_id, "only")
    b_hits = retriever.search_conversation(b.wax_id, "only")
    assert all("alpha" in e.content for e in a_hits)
    assert all("beta" in e.content for e in b_hits)
    assert all(e.wax_id == a.wax_id for e in a_hits)
    assert all(e.wax_id == b.wax_id for e in b_hits)


def test_knowledge_and_notebook_isolation() -> None:
    store = InMemoryStorage()
    knowledge = KnowledgeStore()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    b = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    store.create_student(a)
    store.create_student(b)
    nb = store.get_or_create_notebook(a.wax_id, now)
    store.add_notebook_entry(
        NotebookEntry(
            id=new_id(),
            wax_id=a.wax_id,
            notebook_id=nb.id,
            schema_version=1,
            payload={"note": "a-private-notebook"},
            source_type=None,
            source_ref=None,
            created_at=now,
            updated_at=now,
        )
    )
    knowledge.put_node(
        KnowledgeNode(
            id=new_id(),
            wax_id=a.wax_id,
            node_type="concept",
            label="a-private-node",
            description="secret",
            properties={},
            status=KnowledgeNodeStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    retriever = HybridRetriever(store, knowledge)
    assert retriever.search_notebook(b.wax_id, "private") == ()
    assert retriever.search_knowledge(b.wax_id, "private") == ()
    # Known UUID attack: B cannot inspect A's message id via ownership
    a_conv = Conversation(id=new_id(), wax_id=a.wax_id, created_at=now, updated_at=now)
    store.create_conversation(a_conv)
    a_msg = Message(
        id=new_id(),
        wax_id=a.wax_id,
        conversation_id=a_conv.id,
        role="student",
        content_type=MessageContentType.TEXT,
        text_body="hidden",
        created_at=now,
    )
    store.create_message(a_msg)
    assert retriever.inspect_evidence(b.wax_id, a_msg.id) == ()
