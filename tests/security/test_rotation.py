"""Key rotation regression tests (spec section 14).

Verifies that:
- a new active key can be added
- old rows can be decrypted with their original key
- new data is written with the active key
- old data can be re-encrypted with the new key
- the re-encryption is idempotent (re-running skips already-rotated rows)
- a missing key produces an explicit fail-closed error
- the rotation never logs decrypted student content
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import AuthorKind, HistoryEntry, Student
from waxprep.migrations import apply_migrations
from waxprep.security.cipher import AesGcmFieldCipher, DecryptionError, KeyRing
from waxprep.security.rotation import reencrypt_all, rotate_active_key
from waxprep.storage.postgres import PostgresStorage

NOW = datetime(2026, 11, 1, tzinfo=UTC)
KEY_OLD = b"o" * 32
KEY_NEW = b"n" * 32


@pytest.fixture
def rotated_db() -> Any:
    """A database with data encrypted under key 1, then rotated to key 2."""
    url = "postgresql://postgres@localhost:5432/waxprep_test"
    conn: psycopg.Connection[Any] = psycopg.connect(url, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.commit()
    apply_migrations(conn)
    # Create a student + history entry encrypted with key 1.
    old_cipher = AesGcmFieldCipher(KeyRing(keys={1: KEY_OLD}, active_key_id=1))
    storage = PostgresStorage(conn, old_cipher)
    wax = new_wax_id()
    storage.create_student(Student(wax, NOW, NOW))
    storage.append_history_entry(
        HistoryEntry(
            id=HistoryEntry.__dataclass_fields__["id"].default()  # type: ignore[attr-defined]
            if False
            else __import__("uuid").uuid4(),
            wax_id=wax,
            author_kind=AuthorKind.STUDENT,
            content_kind="text/plain",
            text="secret message under key 1",
            occurred_at=NOW,
            recorded_at=NOW,
        )
    )
    # Rotate to key 2.
    new_ring = rotate_active_key(
        conn,
        new_key_id=2,
        new_key=KEY_NEW,
        existing_keys={1: KEY_OLD},
    )
    new_cipher = AesGcmFieldCipher(new_ring)
    yield conn, old_cipher, new_cipher, wax
    conn.close()


def test_new_active_key_is_used_for_new_writes(rotated_db: Any) -> None:
    """After rotation, new writes use the new active key."""
    conn, old_cipher, new_cipher, wax = rotated_db
    storage = PostgresStorage(conn, new_cipher)
    entry = storage.append_history_entry(
        HistoryEntry(
            id=__import__("uuid").uuid4(),
            wax_id=wax,
            author_kind=AuthorKind.STUDENT,
            content_kind="text/plain",
            text="written under key 2",
            occurred_at=NOW,
            recorded_at=NOW,
        )
    )
    # The ciphertext header's key id must be 2.
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT text_encrypted FROM history_entries WHERE id = %s",
            (entry.id,),
        )
        row = cur.fetchone()
    assert row is not None
    blob = bytes(row[0])
    assert blob[1] == 2, "new writes must use the active key id 2"


def test_old_data_still_readable_after_rotation(rotated_db: Any) -> None:
    """Old rows encrypted with key 1 can still be read because the old
    key remains in the ring."""
    conn, _, new_cipher, wax = rotated_db
    storage = PostgresStorage(conn, new_cipher)
    history = storage.list_history(wax, limit=10)
    assert len(history) == 1
    assert history[0].text == "secret message under key 1"


def test_reencrypt_all_rotates_old_rows(rotated_db: Any) -> None:
    """Re-encrypting all columns updates old rows to use the new key."""
    conn, old_cipher, new_cipher, wax = rotated_db
    # Before re-encryption, the old row's ciphertext uses key 1.
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT text_encrypted FROM history_entries WHERE wax_id = %s",
            (wax,),
        )
        row = cur.fetchone()
    assert row is not None
    assert bytes(row[0])[1] == 1, "old row should still use key 1"
    # Re-encrypt.
    summary = reencrypt_all(conn, new_cipher)
    hist_report = next(r for r in summary.reports if r.table == "history_entries")
    assert hist_report.rows_reencrypted >= 1, (
        f"expected at least 1 row re-encrypted; got {hist_report.rows_reencrypted}"
    )
    # After re-encryption, the row's ciphertext uses key 2.
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT text_encrypted FROM history_entries WHERE wax_id = %s",
            (wax,),
        )
        row = cur.fetchone()
    assert row is not None
    assert bytes(row[0])[1] == 2, "re-encrypted row must use key 2"


def test_reencrypt_is_idempotent(rotated_db: Any) -> None:
    """Re-running re-encryption skips rows already encrypted with the
    active key."""
    conn, _, new_cipher, _ = rotated_db
    # First pass: re-encrypts old rows.
    first = reencrypt_all(conn, new_cipher)
    first_reencrypted = first.total_reencrypted()
    # Second pass: nothing to re-encrypt.
    second = reencrypt_all(conn, new_cipher)
    second_reencrypted = second.total_reencrypted()
    assert second_reencrypted == 0, (
        f"second pass must skip already-rotated rows; got {second_reencrypted}"
    )
    assert first_reencrypted > 0


def test_missing_key_produces_fail_closed_error(rotated_db: Any) -> None:
    """If a key is missing from the ring, decryption fails closed —
    no fabricated content."""
    conn, old_cipher, _, wax = rotated_db
    # Drop key 1 from the ring — the old row can't be decrypted.
    broken_cipher = AesGcmFieldCipher(
        KeyRing(keys={2: KEY_NEW}, active_key_id=2, blind_index_key_id=2)
    )
    storage = PostgresStorage(conn, broken_cipher)
    # Attempting to read the old row must raise DecryptionError, not
    # return fabricated content.
    with pytest.raises(DecryptionError):
        storage.list_history(wax, limit=10)


def test_rotation_never_logs_decrypted_content(rotated_db: Any, caplog: Any) -> None:
    """The rotation log must never contain decrypted student text."""
    conn, _, new_cipher, wax = rotated_db
    with caplog.at_level("INFO"):
        reencrypt_all(conn, new_cipher)
    # The student's message text must not appear in any log message.
    for record in caplog.records:
        assert "secret message under key 1" not in record.getMessage(), (
            "decrypted student content must never be logged"
        )
