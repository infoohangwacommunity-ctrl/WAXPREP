"""Unified history: save everything, first, with real timestamps."""

from __future__ import annotations

from datetime import timedelta

from support import add_student, make_world
from waxprep.domain.models import AuthorKind
from waxprep.storage.memory import InMemoryStorage


def test_both_sides_are_saved_with_who_and_when() -> None:
    world = make_world()
    wax = add_student(world)
    hist = world.services.history
    student = hist.record(wax, AuthorKind.STUDENT, "I don't get quadratics")
    world.clock.advance(5)
    hist.record(
        wax, AuthorKind.TUTOR, "Let's start with factorising", reply_to_id=student.id
    )
    saved = world.storage.list_history(wax)
    assert [e.author_kind for e in saved] == [AuthorKind.STUDENT, AuthorKind.TUTOR]
    assert saved[1].reply_to_id == student.id
    assert saved[1].occurred_at > saved[0].occurred_at


def test_occurred_at_and_recorded_at_are_kept_separately() -> None:
    world = make_world()
    wax = add_student(world)
    sent = world.clock.now() - timedelta(hours=3)  # network was down
    entry = world.services.history.record(
        wax, AuthorKind.STUDENT, "late message", occurred_at=sent
    )
    assert entry.occurred_at == sent
    assert entry.recorded_at == world.clock.now()


def test_nothing_is_judged_important_at_save_time() -> None:
    world = make_world()
    wax = add_student(world)
    for text in ("hello", "I'm hungry", "ok", "🙂", "Solve x^2 - 5x + 6 = 0"):
        world.services.history.record(wax, AuthorKind.STUDENT, text)
    assert len(world.storage.list_history(wax)) == 5


def test_duplicate_channel_delivery_is_saved_once() -> None:
    world = make_world()
    wax = add_student(world)
    first = world.services.history.record(
        wax, AuthorKind.STUDENT, "hi", channel="whatsapp", channel_ref="wamid.1"
    )
    again = world.services.history.record(
        wax, AuthorKind.STUDENT, "hi", channel="whatsapp", channel_ref="wamid.1"
    )
    assert again.id == first.id
    assert len(world.storage.list_history(wax)) == 1


class _BrokenIndexStorage(InMemoryStorage):
    def put_search_tokens(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("index down")

    def enqueue_job(self, job):  # type: ignore[no-untyped-def,override]
        raise RuntimeError("queue down")


def test_failed_indexing_never_loses_the_message() -> None:
    world = make_world(storage=_BrokenIndexStorage())
    wax = add_student(world)
    entry = world.services.history.record(wax, AuthorKind.STUDENT, "keep me")
    stored = world.storage.get_history_entry(wax, entry.id)
    assert stored is not None and stored.text == "keep me"


def test_students_cannot_read_each_others_history() -> None:
    world = make_world()
    a, b = add_student(world), add_student(world)
    entry = world.services.history.record(a, AuthorKind.STUDENT, "only for a")
    assert world.storage.get_history_entry(b, entry.id) is None
    assert world.storage.list_history(b) == ()


def test_history_can_be_read_around_a_moment() -> None:
    world = make_world()
    wax = add_student(world)
    ids = []
    for i in range(7):
        world.clock.advance(60)
        ids.append(world.services.history.record(wax, AuthorKind.STUDENT, f"m{i}").id)
    around = world.storage.list_history_around(wax, ids[3], before=2, after=1)
    assert [e.text for e in around] == ["m1", "m2", "m3", "m4"]


def test_media_without_text_is_still_history() -> None:
    world = make_world()
    wax = add_student(world)
    entry = world.services.history.record(
        wax, AuthorKind.STUDENT, None, content_kind="image/jpeg"
    )
    assert world.storage.get_history_entry(wax, entry.id) is not None


def test_storage_offers_no_way_to_delete_or_edit_history() -> None:
    forbidden = ("delete_history", "update_history", "edit_history", "remove_history")
    for name in dir(InMemoryStorage):
        assert not any(name.startswith(f) for f in forbidden), name
