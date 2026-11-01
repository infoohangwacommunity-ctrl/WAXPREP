"""Student calendar storage tests (Founder's vision §7).

The ``student_calendar`` table holds exam dates, deadlines, school
sessions, and other time-sensitive events. The sweep method
``expire_past_deadlines`` marks past deadlines as expired without
deleting them — temporal truth is a first-class responsibility.

These tests run against the in-memory storage by default. The
postgres-marked variants verify the same contract against a real
PostgreSQL database with RLS + encryption + the date-range and
sweep indexes from migration 021.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    CalendarEvent,
    CalendarEventKind,
    Student,
)
from waxprep.storage.memory import InMemoryStorage
from waxprep.storage.postgres import PostgresStorage

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def _store() -> InMemoryStorage:
    return InMemoryStorage()


def _seed(store: InMemoryStorage) -> Any:
    wax = new_wax_id()
    store.create_student(Student(wax_id=wax, created_at=NOW, updated_at=NOW))
    return wax


def _event(
    wax: Any,
    *,
    kind: CalendarEventKind = CalendarEventKind.DEADLINE,
    date: datetime,
    subject: str | None = None,
    notes: str | None = None,
) -> CalendarEvent:
    from waxprep.domain.models import new_id

    return CalendarEvent(
        id=new_id(),
        wax_id=wax,
        event_kind=kind,
        event_date=date,
        subject=subject,
        notes=notes,
        created_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# In-memory: create + retrieve
# ---------------------------------------------------------------------------


def test_add_and_list_calendar_event_round_trip() -> None:
    """Adding an event then retrieving the calendar returns the event
    with the same fields. The event_date is preserved as TIMESTAMPTZ
    (UTC) so the runtime can convert to the student's local timezone
    for presentation."""
    store = _store()
    wax = _seed(store)
    deadline = NOW + timedelta(days=3)
    event = store.add_calendar_event(
        _event(
            wax,
            kind=CalendarEventKind.DEADLINE,
            date=deadline,
            subject="Mathematics",
            notes="covers chapters 4-6",
        )
    )
    fetched = store.get_student_calendar(wax)
    assert len(fetched) == 1
    assert fetched[0].id == event.id
    assert fetched[0].event_kind is CalendarEventKind.DEADLINE
    assert fetched[0].event_date == deadline
    assert fetched[0].subject == "Mathematics"
    assert fetched[0].notes == "covers chapters 4-6"


def test_calendar_supports_all_event_kinds() -> None:
    """Every event_kind from the Founder's brief (exam / deadline /
    session / other) round-trips through storage."""
    store = _store()
    wax = _seed(store)
    for kind in (
        CalendarEventKind.EXAM,
        CalendarEventKind.DEADLINE,
        CalendarEventKind.SESSION,
        CalendarEventKind.OTHER,
    ):
        store.add_calendar_event(
            _event(wax, kind=kind, date=NOW + timedelta(days=1))
        )
    fetched = store.get_student_calendar(wax)
    assert len(fetched) == 4
    assert {e.event_kind for e in fetched} == {
        CalendarEventKind.EXAM,
        CalendarEventKind.DEADLINE,
        CalendarEventKind.SESSION,
        CalendarEventKind.OTHER,
    }


def test_calendar_range_filter_half_open() -> None:
    """get_student_calendar(from_date, to_date) returns events in
    [from_date, to_date). The half-open interval matches the
    convention used by search_history_by_time_range."""
    store = _store()
    wax = _seed(store)
    dates = [
        NOW,
        NOW + timedelta(days=1),
        NOW + timedelta(days=2),
        NOW + timedelta(days=3),
    ]
    for d in dates:
        store.add_calendar_event(_event(wax, date=d))
    fetched = store.get_student_calendar(
        wax,
        from_date=NOW + timedelta(days=1),
        to_date=NOW + timedelta(days=3),
    )
    assert [e.event_date for e in fetched] == [
        NOW + timedelta(days=1),
        NOW + timedelta(days=2),
    ]


def test_calendar_orders_by_event_date_ascending() -> None:
    """Events are returned in ascending event_date order so the CI can
    walk through time naturally: 'what's coming up next?'."""
    store = _store()
    wax = _seed(store)
    # Insert out of order.
    for d in [
        NOW + timedelta(days=5),
        NOW + timedelta(days=1),
        NOW + timedelta(days=3),
    ]:
        store.add_calendar_event(_event(wax, date=d))
    fetched = store.get_student_calendar(wax)
    assert [e.event_date for e in fetched] == [
        NOW + timedelta(days=1),
        NOW + timedelta(days=3),
        NOW + timedelta(days=5),
    ]


# ---------------------------------------------------------------------------
# In-memory: expire_past_deadlines sweep
# ---------------------------------------------------------------------------


def test_expire_past_deadlines_marks_only_past_deadlines() -> None:
    """The sweep marks every deadline whose event_date < now as
    expired. Exams and sessions are NOT swept — an exam that has
    passed is still a fact about the student's calendar, not an
    "expired deadline" in the temporal-truth sense the Founder means."""
    store = _store()
    wax = _seed(store)
    past_deadline = NOW - timedelta(days=1)
    future_deadline = NOW + timedelta(days=1)
    past_exam = NOW - timedelta(days=2)
    future_session = NOW + timedelta(days=2)
    for kind, date in (
        (CalendarEventKind.DEADLINE, past_deadline),
        (CalendarEventKind.DEADLINE, future_deadline),
        (CalendarEventKind.EXAM, past_exam),
        (CalendarEventKind.SESSION, future_session),
    ):
        store.add_calendar_event(_event(wax, kind=kind, date=date))
    count = store.expire_past_deadlines(NOW)
    # Only one row should be marked: the past deadline.
    assert count == 1
    fetched = store.get_student_calendar(wax, include_expired=False)
    # The past deadline is excluded from the default view.
    assert len(fetched) == 3
    # ...but is still reachable via include_expired=True.
    with_expired = store.get_student_calendar(wax, include_expired=True)
    assert len(with_expired) == 4
    past = next(
        e for e in with_expired if e.expired_at is not None
    )
    assert past.event_kind is CalendarEventKind.DEADLINE
    assert past.event_date == past_deadline
    assert past.expired_at == NOW


def test_expire_past_deadlines_is_idempotent() -> None:
    """Running the sweep twice does not double-mark: the second run
    finds no rows whose expired_at IS NULL."""
    store = _store()
    wax = _seed(store)
    store.add_calendar_event(
        _event(wax, date=NOW - timedelta(days=1))
    )
    first = store.expire_past_deadlines(NOW)
    second = store.expire_past_deadlines(NOW)
    assert first == 1
    assert second == 0


def test_expire_past_deadlines_is_cross_tenant() -> None:
    """The sweep operates across every student — the Founder's
    temporal-truth contract is global. In the in-memory store this
    is a single pass over the events map; in Postgres it runs as a
    RESET ROLE sweep that sees every student's deadlines."""
    store = _store()
    a = _seed(store)
    b = new_wax_id()
    store.create_student(Student(wax_id=b, created_at=NOW, updated_at=NOW))
    store.add_calendar_event(
        _event(a, date=NOW - timedelta(days=1))
    )
    store.add_calendar_event(
        _event(b, date=NOW - timedelta(days=1))
    )
    # One sweep, two students affected.
    count = store.expire_past_deadlines(NOW)
    assert count == 2


# ---------------------------------------------------------------------------
# In-memory: RLS isolation
# ---------------------------------------------------------------------------


def test_student_a_cannot_see_student_b_calendar_in_memory() -> None:
    """The in-memory store keys every event by (wax_id, id). A query
    for student B's calendar must return an empty tuple — student A's
    events are invisible to B.

    In Postgres this is enforced by RLS (see the postgres-marked test
    below); in memory it is enforced by the per-wax_id keying.
    """
    store = _store()
    a = _seed(store)
    b = new_wax_id()
    store.create_student(Student(wax_id=b, created_at=NOW, updated_at=NOW))
    store.add_calendar_event(
        _event(
            a,
            kind=CalendarEventKind.EXAM,
            date=NOW + timedelta(days=5),
            subject="Chemistry",
            notes="midterm",
        )
    )
    assert store.get_student_calendar(b) == ()


# ---------------------------------------------------------------------------
# Postgres: same contract against a real DB with RLS + encryption + indexes
# ---------------------------------------------------------------------------


def _pg_student(store: PostgresStorage) -> Any:
    wax = new_wax_id()
    store.create_student(Student(wax_id=wax, created_at=NOW, updated_at=NOW))
    return wax


@pytest.mark.postgres
def test_postgres_add_and_get_calendar_event_round_trip(
    pg_store: PostgresStorage,
) -> None:
    """Postgres round-trip: the notes survive AES-GCM encryption
    (purpose 'student_calendar.notes' from migration 021 column
    comment). The event_date is plaintext so range queries run
    server-side as index scans."""
    wax = _pg_student(pg_store)
    deadline = NOW + timedelta(days=3)
    pg_store.add_calendar_event(
        _event(
            wax,
            kind=CalendarEventKind.DEADLINE,
            date=deadline,
            subject="Mathematics",
            notes="covers chapters 4-6",
        )
    )
    fetched = pg_store.get_student_calendar(wax)
    assert len(fetched) == 1
    assert fetched[0].event_kind is CalendarEventKind.DEADLINE
    assert fetched[0].event_date == deadline
    assert fetched[0].subject == "Mathematics"
    assert fetched[0].notes == "covers chapters 4-6"


@pytest.mark.postgres
def test_postgres_expire_past_deadlines_sweep(
    pg_store: PostgresStorage,
) -> None:
    """Postgres sweep: the RESET-ROLE UPDATE finds every past deadline
    across every student. RLS would otherwise blind the waxprep_app
    role to other students' rows; the sweep must be cross-tenant
    (Founder's vision §7-8)."""
    a = _pg_student(pg_store)
    b = _pg_student(pg_store)
    pg_store.add_calendar_event(
        _event(a, date=NOW - timedelta(days=1))
    )
    pg_store.add_calendar_event(
        _event(b, date=NOW - timedelta(days=2))
    )
    pg_store.add_calendar_event(
        _event(a, date=NOW + timedelta(days=1))
    )
    count = pg_store.expire_past_deadlines(NOW)
    assert count == 2  # both past deadlines, across two students


@pytest.mark.postgres
def test_postgres_rls_blocks_cross_student_calendar(
    pg_store: PostgresStorage,
) -> None:
    """RLS isolation: student B's get_student_calendar must return an
    empty tuple even when student A has events."""
    a = _pg_student(pg_store)
    b = _pg_student(pg_store)
    pg_store.add_calendar_event(
        _event(
            a,
            kind=CalendarEventKind.EXAM,
            date=NOW + timedelta(days=5),
            subject="Chemistry",
            notes="midterm",
        )
    )
    assert pg_store.get_student_calendar(b) == ()


@pytest.mark.postgres
def test_postgres_set_student_timezone_round_trip(
    pg_store: PostgresStorage,
) -> None:
    """set_student_timezone persists the IANA timezone; get_student
    returns it. The Founder's vision §7: clock.student_local_now
    reads this column to convert UTC now() to the student's wall clock."""
    wax = _pg_student(pg_store)
    updated = pg_store.set_student_timezone(wax, "Africa/Lagos", NOW)
    assert updated is not None
    assert updated.timezone == "Africa/Lagos"
    fetched = pg_store.get_student(wax)
    assert fetched is not None
    assert fetched.timezone == "Africa/Lagos"


@pytest.mark.postgres
def test_postgres_set_student_timezone_can_clear(
    pg_store: PostgresStorage,
) -> None:
    """Passing None clears the timezone. CI may need to revise its
    belief (e.g. the student said 'my exam is at 9am tomorrow' but
    the inferred timezone was wrong)."""
    wax = _pg_student(pg_store)
    pg_store.set_student_timezone(wax, "Africa/Lagos", NOW)
    cleared = pg_store.set_student_timezone(wax, None, NOW)
    assert cleared is not None
    assert cleared.timezone is None


# Suppress unused-import warnings for fixtures used only by postgres-marked
# tests.
_ = PostgresStorage
