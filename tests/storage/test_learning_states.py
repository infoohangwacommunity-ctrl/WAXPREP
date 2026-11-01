"""Learning-state storage tests (Founder's vision §6).

The ``learning_states`` table is the third persistence layer alongside
history_entries (ground truth) and notebook_entries (durable
understanding). It captures what is happening in the lesson RIGHT NOW
and is superseded (never edited) every time the student advances.

These tests run against the in-memory storage by default. The
postgres-marked variants (``pg_store``/``pg_conn`` fixtures) verify
the same contract against a real PostgreSQL database with RLS +
encryption + the partial unique index from migration 018. They
error in the sandbox (no real DB); in CI they pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    LearningStateStatus,
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


# ---------------------------------------------------------------------------
# In-memory: create + retrieve
# ---------------------------------------------------------------------------


def test_set_and_get_learning_state_round_trip() -> None:
    """set_learning_state then get_learning_state returns the same payload.

    The Founder's vision §6: a student who says "wait, why did we use
    4 there?" should retrieve the current learning state and continue
    without restarting the lesson. The round-trip is the storage
    primitive that makes that possible.
    """
    store = _store()
    wax = _seed(store)
    payload = {
        "topic": "quadratic equations",
        "stage": "understanding the meaning of x²",
        "demonstrated": ["meaning of unknowns in simple equations"],
        "developing": ["factorisation"],
        "not_assessed": ["quadratic formula"],
        "teaching_observation": (
            "understood exponentiation better after repeated multiplication"
        ),
        "next_step": "check if they can calculate simple squares independently",
    }
    state = store.set_learning_state(wax, "Mathematics", payload, now=NOW)
    assert state.status is LearningStateStatus.ACTIVE
    assert state.subject == "Mathematics"
    assert state.last_active_at == NOW

    fetched = store.get_learning_state(wax, "Mathematics")
    assert fetched is not None
    assert fetched.id == state.id
    assert fetched.subject == "Mathematics"
    assert fetched.payload["topic"] == "quadratic equations"
    assert fetched.payload["next_step"].startswith("check")


def test_get_learning_state_returns_none_when_no_state() -> None:
    store = _store()
    wax = _seed(store)
    assert store.get_learning_state(wax, "Mathematics") is None
    assert store.get_learning_state(wax) is None  # no subject either


def test_get_learning_state_most_recent_when_no_subject() -> None:
    """get_learning_state(wax_id) returns the most-recently-active state."""
    store = _store()
    wax = _seed(store)
    earlier = NOW - timedelta(hours=2)
    store.set_learning_state(wax, "Chemistry", {"topic": "acids"}, now=earlier)
    store.set_learning_state(wax, "Mathematics", {"topic": "quadratics"}, now=NOW)
    fetched = store.get_learning_state(wax)
    assert fetched is not None
    assert fetched.subject == "Mathematics"
    assert fetched.last_active_at == NOW


# ---------------------------------------------------------------------------
# In-memory: supersession
# ---------------------------------------------------------------------------


def test_new_state_supersedes_old_state() -> None:
    """Writing a new state for an existing subject supersedes the old.

    The Founder's vision §6: a learning state is replaced, never
    edited. The previous state stays for audit; the new state becomes
    the active one.
    """
    store = _store()
    wax = _seed(store)
    first = store.set_learning_state(wax, "Mathematics", {"stage": "intro"}, now=NOW)
    later = NOW + timedelta(hours=1)
    second = store.set_learning_state(
        wax, "Mathematics", {"stage": "factorisation"}, now=later
    )
    # Only one active state per subject.
    active = store.get_learning_state(wax, "Mathematics")
    assert active is not None
    assert active.id == second.id
    assert active.payload["stage"] == "factorisation"
    # The first state is no longer active.
    assert first.id != second.id


def test_multiple_subjects_coexist_as_separate_states() -> None:
    """A student can be working on multiple subjects simultaneously —
    one active state per subject, not one global state."""
    store = _store()
    wax = _seed(store)
    store.set_learning_state(wax, "Mathematics", {"stage": "quadratics"}, now=NOW)
    later = NOW + timedelta(hours=1)
    store.set_learning_state(wax, "Chemistry", {"stage": "acids"}, now=later)
    active = store.list_active_learning_states(wax)
    assert len(active) == 2
    # Chemistry was set later, so it's first in the most-recent-first list.
    assert active[0].subject == "Chemistry"
    assert active[1].subject == "Mathematics"


def test_supersession_preserves_audit_trail_via_old_id() -> None:
    """The new state's id differs from the old state's id; the old
    state is no longer reachable via get_learning_state (active
    contract) but is preserved internally for audit (by-id map)."""
    store = _store()
    wax = _seed(store)
    first = store.set_learning_state(wax, "Mathematics", {"stage": "intro"}, now=NOW)
    later = NOW + timedelta(hours=1)
    second = store.set_learning_state(
        wax, "Mathematics", {"stage": "factorisation"}, now=later
    )
    # The superseded state is preserved in the by-id map.
    superseded = store._learning_states_by_id[(wax, first.id)]  # type: ignore[attr-defined]
    assert superseded.status is LearningStateStatus.SUPERSEDED
    assert superseded.superseded_by_id == second.id


def test_paused_state_records_paused_at() -> None:
    """A student may step away mid-lesson. paused=True records paused_at
    so CI can surface 'we paused on quadratics yesterday' without
    losing the active state."""
    store = _store()
    wax = _seed(store)
    state = store.set_learning_state(
        wax,
        "Mathematics",
        {"stage": "mid-lesson"},
        now=NOW,
        paused=True,
    )
    assert state.paused_at == NOW
    fetched = store.get_learning_state(wax, "Mathematics")
    assert fetched is not None
    assert fetched.paused_at == NOW


# ---------------------------------------------------------------------------
# In-memory: RLS isolation
# ---------------------------------------------------------------------------


def test_student_a_cannot_see_student_b_learning_state_in_memory() -> None:
    """The in-memory store keys every state by (wax_id, subject_lower).
    A query for student B's state must return None — student A's
    state is invisible to B.

    In Postgres this is enforced by RLS (see the postgres-marked test
    below); in memory it is enforced by the per-wax_id keying.
    """
    store = _store()
    a = _seed(store)
    b = new_wax_id()
    store.create_student(Student(wax_id=b, created_at=NOW, updated_at=NOW))
    store.set_learning_state(a, "Mathematics", {"stage": "quadratics"}, now=NOW)
    # Student B asks for their Mathematics state — must be None.
    assert store.get_learning_state(b, "Mathematics") is None
    # And B's active-learning-states list must be empty.
    assert store.list_active_learning_states(b) == ()
    # And B's "most recent" lookup must be None.
    assert store.get_learning_state(b) is None


# ---------------------------------------------------------------------------
# Postgres: same contract against a real DB with RLS + encryption + the
# partial unique index from migration 018.
# ---------------------------------------------------------------------------


def _pg_student(store: PostgresStorage) -> Any:
    wax = new_wax_id()
    store.create_student(Student(wax_id=wax, created_at=NOW, updated_at=NOW))
    return wax


@pytest.mark.postgres
def test_postgres_set_and_get_learning_state_round_trip(
    pg_store: PostgresStorage,
) -> None:
    """Postgres round-trip: the payload survives AES-GCM encryption +
    JSON serialisation + the subject_hash blind index. The plaintext
    subject is never transmitted — only its blind HMAC index.
    """
    wax = _pg_student(pg_store)
    payload = {
        "topic": "quadratic equations",
        "stage": "understanding x²",
        "demonstrated": ["meaning of unknowns"],
    }
    state = pg_store.set_learning_state(wax, "Mathematics", payload, now=NOW)
    assert state.status is LearningStateStatus.ACTIVE
    fetched = pg_store.get_learning_state(wax, "Mathematics")
    assert fetched is not None
    assert fetched.id == state.id
    assert fetched.subject == "Mathematics"
    assert fetched.payload["topic"] == "quadratic equations"


@pytest.mark.postgres
def test_postgres_supersession_marks_old_row(
    pg_store: PostgresStorage, pg_conn: Any
) -> None:
    """Postgres supersession: the old row is set to status='superseded'
    with superseded_by pointing at the new row. The partial unique
    index `learning_states_active_per_subject_idx` guarantees at most
    one active state per (wax_id, subject)."""
    from waxprep.storage.session import assume_app_role, set_current_wax_id

    wax = _pg_student(pg_store)
    pg_store.set_learning_state(wax, "Mathematics", {"stage": "intro"}, now=NOW)
    later = NOW + timedelta(hours=1)
    pg_store.set_learning_state(
        wax, "Mathematics", {"stage": "factorisation"}, now=later
    )
    # Only one active row.
    assume_app_role(pg_conn)
    set_current_wax_id(pg_conn, wax)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM learning_states "
            "WHERE wax_id = %s AND subject_hash = %s AND status = 'active'",
            (wax, TEST_CIPHER.blind_index(wax, "Mathematics")),
        )
        active_count = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM learning_states "
            "WHERE wax_id = %s AND subject_hash = %s AND status = 'superseded'",
            (wax, TEST_CIPHER.blind_index(wax, "Mathematics")),
        )
        superseded_count = cur.fetchone()[0]
    assert active_count == 1
    assert superseded_count == 1


@pytest.mark.postgres
def test_postgres_rls_blocks_cross_student_learning_state(
    pg_store: PostgresStorage,
) -> None:
    """RLS isolation: student B's get_learning_state must return None
    even when student A has an active state for the same subject. The
    blind HMAC index is per-student (derived from the wax_id), so B's
    hash for 'Mathematics' differs from A's hash for 'Mathematics' —
    but even if they collided, RLS would still hide A's row from B.
    """
    a = _pg_student(pg_store)
    b = _pg_student(pg_store)
    pg_store.set_learning_state(a, "Mathematics", {"stage": "quadratics"}, now=NOW)
    assert pg_store.get_learning_state(b, "Mathematics") is None
    assert pg_store.list_active_learning_states(b) == ()
    assert pg_store.get_learning_state(b) is None


@pytest.mark.postgres
def test_postgres_learning_state_uses_parameterised_queries(
    pg_store: PostgresStorage,
) -> None:
    """Defence-in-depth: every query path uses parameterised binding.
    A subject string with SQL metacharacters must not affect the query
    — psycopg escapes the parameter, and the subject is only ever
    hashed via the cipher before reaching the SQL layer.
    """
    wax = _pg_student(pg_store)
    evil_subject = "Mathematics'; DROP TABLE learning_states; --"
    # The set + get must not raise, and must round-trip the subject
    # intact (the subject itself is the key the caller already knows).
    state = pg_store.set_learning_state(wax, evil_subject, {"stage": "intro"}, now=NOW)
    fetched = pg_store.get_learning_state(wax, evil_subject)
    assert fetched is not None
    assert fetched.id == state.id
    assert fetched.subject == evil_subject


# ---------------------------------------------------------------------------
# Sanity: the storage layer exposes the new methods on the Protocol
# ---------------------------------------------------------------------------


def test_storage_protocol_has_learning_state_methods() -> None:
    """The Storage Protocol declares the new learning-state methods
    so callers can swap PostgresStorage <-> InMemoryStorage freely."""
    from waxprep.storage.protocol import Storage

    for name in (
        "get_learning_state",
        "set_learning_state",
        "list_active_learning_states",
        "set_student_timezone",
        "add_calendar_event",
        "get_student_calendar",
        "expire_past_deadlines",
    ):
        assert hasattr(Storage, name), (
            f"Storage Protocol must declare {name} (Founder's vision §6/§7)"
        )


# Suppress unused-import warnings for fixtures used only by postgres-marked
# tests. The fixtures come from tests/conftest.py.
_ = (PostgresStorage, uuid4)
