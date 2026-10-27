"""Durability regressions: idempotent provisioning and orphan pruning.

Live incident (2026-10-26): Meta retried WhatsApp webhooks after 500s.
Provisioning wrote the student and the channel identity in two separate
commits, so a crash between them left orphan students, and every retry
added another one. These tests pin the fixes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from psycopg.pq import TransactionStatus

from support import TEST_CIPHER
from waxprep.app import _prune_orphan_students, provision_channel_student
from waxprep.clock import FakeClock
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    ChannelIdentity,
    HistoryEntry,
    Student,
    new_id,
)
from waxprep.storage.memory import InMemoryStorage
from waxprep.storage.postgres import PostgresStorage

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(hours=2)


def _identity(wax_id: WaxId) -> ChannelIdentity:
    return ChannelIdentity(
        id=new_id(),
        wax_id=wax_id,
        channel="whatsapp",
        external_id="+234",
        created_at=OLD,
    )


def test_provision_is_idempotent() -> None:
    storage = InMemoryStorage()
    clock = FakeClock(NOW)
    first = provision_channel_student(storage, clock, "whatsapp", "+234")
    second = provision_channel_student(storage, clock, "whatsapp", "+234")
    assert first == second
    identity = storage.get_channel_identity("whatsapp", "+234")
    assert identity is not None
    assert identity.wax_id == first


def test_failed_provision_leaves_no_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = InMemoryStorage()
    clock = FakeClock(NOW)

    def boom(identity: ChannelIdentity) -> ChannelIdentity:
        raise RuntimeError("identity insert failed")

    monkeypatch.setattr(storage, "create_channel_identity", boom)
    with pytest.raises(RuntimeError):
        provision_channel_student(storage, clock, "whatsapp", "+234")
    assert storage.get_channel_identity("whatsapp", "+234") is None
    # Nothing was left behind: no orphan exists even with a cutoff in
    # the future (every student would count as "older than").
    assert storage.list_orphan_student_ids(NOW + timedelta(hours=1)) == ()


def test_orphan_scan_finds_only_abandoned_students() -> None:
    storage = InMemoryStorage()
    orphan = new_wax_id()
    storage.create_student(Student(orphan, OLD, OLD))
    linked = new_wax_id()
    storage.create_student(Student(linked, OLD, OLD))
    storage.create_channel_identity(_identity(linked))
    busy = new_wax_id()
    storage.create_student(Student(busy, OLD, OLD))
    storage.append_history_entry(
        HistoryEntry(
            new_id(), busy, AuthorKind.STUDENT, "text/plain", "hi", OLD, OLD
        )
    )
    fresh = new_wax_id()
    storage.create_student(Student(fresh, NOW, NOW))

    assert storage.list_orphan_student_ids(NOW) == (orphan,)
    assert storage.delete_student(orphan) is True
    assert storage.delete_student(orphan) is False
    assert storage.get_student(linked) is not None
    assert storage.get_student(busy) is not None
    assert storage.list_orphan_student_ids(NOW) == ()


def test_prune_removes_only_orphans() -> None:
    storage = InMemoryStorage()
    orphan = new_wax_id()
    storage.create_student(Student(orphan, OLD, OLD))
    keep = new_wax_id()
    storage.create_student(Student(keep, OLD, OLD))
    storage.create_channel_identity(_identity(keep))

    _prune_orphan_students(storage, FakeClock(NOW))

    assert storage.get_student(orphan) is None
    assert storage.get_student(keep) is not None


def test_storage_heals_poisoned_connection() -> None:
    class FakeInfo:
        def __init__(self) -> None:
            self.transaction_status: int = TransactionStatus.IDLE

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            return None

        def fetchone(self) -> object:
            return None

    class FakeConn:
        def __init__(self) -> None:
            self.info = FakeInfo()
            self.rollbacks = 0

        def cursor(self, *args: object, **kwargs: object) -> FakeCursor:
            return FakeCursor()

        def rollback(self) -> None:
            self.rollbacks += 1
            self.info.transaction_status = TransactionStatus.IDLE

        def commit(self) -> None:
            return None

    conn = FakeConn()
    store = PostgresStorage(cast(Any, conn), TEST_CIPHER)
    # Simulate a failed statement that left the connection aborted.
    conn.info.transaction_status = TransactionStatus.INERROR
    assert store.get_channel_identity("whatsapp", "+234") is None
    assert conn.rollbacks == 1
    # A healthy connection is not rolled back again.
    assert store.get_channel_identity("whatsapp", "+234") is None
    assert conn.rollbacks == 1
