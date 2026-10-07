"""Storage contract tests (in-memory)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from waxprep.clock import FakeClock
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Attachment,
    Conversation,
    Event,
    Message,
    MessageContentType,
    NotebookEntry,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.storage.memory import InMemoryStorage


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 3, 1, 10, 0, tzinfo=UTC))


@pytest.fixture
def store() -> InMemoryStorage:
    return InMemoryStorage()


def _student(clock: FakeClock) -> Student:
    now = clock.now()
    return Student(
        wax_id=new_wax_id(),
        created_at=now,
        updated_at=now,
        status=StudentStatus.ACTIVE,
    )


def test_student_create_and_get(store: InMemoryStorage, clock: FakeClock) -> None:
    student = _student(clock)
    store.create_student(student)
    loaded = store.get_student(student.wax_id)
    assert loaded is not None
    assert loaded.wax_id == student.wax_id


def test_conversation_and_message_ownership(
    store: InMemoryStorage, clock: FakeClock
) -> None:
    student = _student(clock)
    store.create_student(student)
    conv = Conversation(
        id=new_id(),
        wax_id=student.wax_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.create_conversation(conv)
    msg = Message(
        id=new_id(),
        wax_id=student.wax_id,
        conversation_id=conv.id,
        role="student",
        content_type=MessageContentType.TEXT,
        text_body="hello",
        created_at=clock.now(),
    )
    store.create_message(msg)
    other = new_wax_id()
    assert store.get_conversation(other, conv.id) is None
    assert store.get_message(other, conv.id, msg.id) is None
    assert store.get_message(student.wax_id, conv.id, msg.id) is not None


def test_attachment_chain(store: InMemoryStorage, clock: FakeClock) -> None:
    student = _student(clock)
    store.create_student(student)
    conv = Conversation(
        id=new_id(),
        wax_id=student.wax_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.create_conversation(conv)
    msg = Message(
        id=new_id(),
        wax_id=student.wax_id,
        conversation_id=conv.id,
        role="student",
        content_type=MessageContentType.IMAGE,
        text_body=None,
        created_at=clock.now(),
    )
    store.create_message(msg)
    att = Attachment(
        id=new_id(),
        wax_id=student.wax_id,
        message_id=msg.id,
        media_category=MessageContentType.IMAGE,
        mime_type="image/jpeg",
        storage_ref="blob://example",
        size_bytes=100,
        created_at=clock.now(),
    )
    store.create_attachment(att)
    assert store.get_attachment(student.wax_id, att.id) is not None
    assert store.get_attachment(new_wax_id(), att.id) is None


def test_notebook_open_ended_entry(store: InMemoryStorage, clock: FakeClock) -> None:
    student = _student(clock)
    store.create_student(student)
    notebook = store.get_or_create_notebook(student.wax_id, clock.now())
    entry = NotebookEntry(
        id=new_id(),
        wax_id=student.wax_id,
        notebook_id=notebook.id,
        schema_version=1,
        payload={
            "observation": (
                "The student connected this idea to repairing a motorcycle."
            ),
        },
        source_type="conversation",
        source_ref=str(new_id()),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.add_notebook_entry(entry)
    entries = store.list_notebook_entries(student.wax_id, notebook.id)
    assert "motorcycle" in entries[0].payload["observation"]
    assert "goals" not in entries[0].payload
    assert "weaknesses" not in entries[0].payload


def test_events_scoped(store: InMemoryStorage, clock: FakeClock) -> None:
    student = _student(clock)
    store.create_student(student)
    event = Event(
        id=new_id(),
        wax_id=student.wax_id,
        kind="student.created",
        object_type="student",
        object_id=student.wax_id,
        created_at=clock.now(),
        metadata={},
    )
    store.append_event(event)
    assert len(store.list_events(student.wax_id)) == 1
    assert store.list_events(new_wax_id()) == ()


def test_student_data_isolation(store: InMemoryStorage, clock: FakeClock) -> None:
    """Student A cannot access Student B data."""
    a = _student(clock)
    clock.advance(1)
    b = _student(clock)
    store.create_student(a)
    store.create_student(b)
    conv_a = Conversation(
        id=new_id(),
        wax_id=a.wax_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    store.create_conversation(conv_a)
    msg_a = Message(
        id=new_id(),
        wax_id=a.wax_id,
        conversation_id=conv_a.id,
        role="student",
        content_type=MessageContentType.TEXT,
        text_body="private-a",
        created_at=clock.now(),
    )
    store.create_message(msg_a)
    nb_a = store.get_or_create_notebook(a.wax_id, clock.now())
    store.add_notebook_entry(
        NotebookEntry(
            id=new_id(),
            wax_id=a.wax_id,
            notebook_id=nb_a.id,
            schema_version=1,
            payload={"note": "only for A"},
            source_type=None,
            source_ref=None,
            created_at=clock.now(),
            updated_at=clock.now(),
        )
    )
    assert store.get_conversation(b.wax_id, conv_a.id) is None
    assert store.get_message(b.wax_id, conv_a.id, msg_a.id) is None
    assert store.list_notebook_entries(b.wax_id, nb_a.id) == ()
    assert store.get_message(a.wax_id, conv_a.id, msg_a.id) is not None
