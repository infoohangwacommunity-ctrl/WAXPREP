"""Storage contract (in-memory): identity, notebook, attachments, events, isolation."""

from __future__ import annotations

from support import add_student, make_world
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    Attachment,
    AuthorKind,
    Event,
    NotebookEntry,
    new_id,
)


def test_student_create_and_get() -> None:
    world = make_world()
    wax = add_student(world)
    assert world.storage.get_student(wax) is not None
    assert world.storage.get_student(new_wax_id()) is None


def test_attachment_hangs_off_a_history_entry() -> None:
    world = make_world()
    wax = add_student(world)
    entry = world.services.history.record(
        wax, AuthorKind.STUDENT, None, content_kind="image/jpeg"
    )
    att = Attachment(
        new_id(),
        wax,
        entry.id,
        "image",
        "image/jpeg",
        "blob://x",
        100,
        world.clock.now(),
    )
    world.storage.create_attachment(att)
    assert world.storage.get_attachment(wax, att.id) is not None
    assert world.storage.get_attachment(new_wax_id(), att.id) is None
    assert [a.id for a in world.storage.list_attachments_for_entry(wax, entry.id)] == [
        att.id
    ]


def test_notebook_stays_open_ended() -> None:
    world = make_world()
    wax = add_student(world)
    nb = world.storage.get_or_create_notebook(wax, world.clock.now())
    now = world.clock.now()
    world.storage.add_notebook_entry(
        NotebookEntry(
            new_id(),
            wax,
            nb.id,
            1,
            {"observation": "connected it to repairing a motorcycle"},
            now,
            now,
        )
    )
    entry = world.storage.list_notebook_entries(wax, nb.id)[0]
    assert "motorcycle" in entry.payload["observation"]
    assert not {"goals", "weaknesses", "strengths"} & set(entry.payload)


def test_events_scoped_to_one_student() -> None:
    world = make_world()
    wax = add_student(world)
    world.storage.append_event(
        Event(new_id(), wax, "student.created", "student", wax, world.clock.now())
    )
    assert len(world.storage.list_events(wax)) == 1
    assert world.storage.list_events(new_wax_id()) == ()


def test_notebook_isolation() -> None:
    world = make_world()
    a, b = add_student(world), add_student(world)
    nb = world.storage.get_or_create_notebook(a, world.clock.now())
    now = world.clock.now()
    world.storage.add_notebook_entry(
        NotebookEntry(new_id(), a, nb.id, 1, {"n": "only A"}, now, now)
    )
    assert world.storage.list_notebook_entries(b, nb.id) == ()
