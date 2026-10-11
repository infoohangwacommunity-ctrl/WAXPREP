"""Blocker J acceptance tests (spec §8.3 items 12 and 13):
- A `last_error`, event metadata field, or application log cannot expose
  plaintext student content.
- A key rotation/back-up recovery exercise can decrypt old records and
  write new records under the active key; a missing key produces an
  explicit fail-closed error, not blank or fabricated context.
"""

from __future__ import annotations

import pytest

from support import TEST_CIPHER, add_student, make_world
from waxprep.derive.worker import sanitize_error
from waxprep.domain.models import AuthorKind, JobStatus
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.security.cipher import (
    AesGcmFieldCipher,
    DecryptionError,
    KeyRing,
)
from waxprep.storage.memory import InMemoryStorage


class _ExplodingEmbedder(DeterministicEmbeddingProvider):
    async def embed_many(self, texts: list[str]) -> list[tuple[float, ...]]:
        raise ValueError(
            f"could not embed student text: '{texts[0]}' contains sensitive data"
        )


@pytest.mark.asyncio
async def test_job_error_does_not_leak_student_text() -> None:
    """§8.3 item 12: last_error cannot expose plaintext student content."""
    storage = InMemoryStorage()
    world = make_world(storage=storage, embedder=_ExplodingEmbedder())
    wax = add_student(world)
    student_text = "my SSN is 123-45-6789"
    world.services.history.record(
        wax, AuthorKind.STUDENT, student_text
    )
    report = await world.services.worker.run_once()
    assert report.retrying >= 1
    jobs = storage.list_jobs(wax)
    # The job is re-queued as PENDING for retry, but last_error must
    # not contain the student's actual content (SSN, message text).
    assert jobs, "the embedder failure must leave a job behind"
    for job in jobs:
        assert "123-45-6789" not in (job.last_error or ""), (
            f"last_error must not contain student SSN; got: {job.last_error!r}"
        )
        assert "my SSN is" not in (job.last_error or ""), (
            f"last_error must not contain student text; got: {job.last_error!r}"
        )


def test_sanitize_error_strips_quoted_strings_and_long_tokens() -> None:
    """The sanitizer reduces exception text to an opaque marker."""
    exc = ValueError("could not process 'Adeyemi exam is in three months' and 0123456789abcdef0123456789")
    sanitized = sanitize_error(exc)
    assert "Adeyemi" not in sanitized
    assert "exam is in three months" not in sanitized
    assert "0123456789abcdef0123" not in sanitized
    # The exception class name is preserved for debugging.
    assert "ValueError" in sanitized


def test_missing_key_produces_fail_closed_error() -> None:
    """§8.3 item 13: a missing key produces an explicit fail-closed error,
    not blank or fabricated context."""
    cipher = AesGcmFieldCipher(KeyRing(keys={1: b"a" * 32, 2: b"b" * 32}, active_key_id=2))
    import uuid

    from waxprep.domain.identifiers import WaxId

    wax_id = WaxId(uuid.uuid4())
    blob = cipher.encrypt_text(wax_id, "test.purpose", "secret message")
    # Drop key 2 from the ring — decryption must fail closed.
    broken_cipher = AesGcmFieldCipher(KeyRing(keys={1: b"a" * 32}, active_key_id=1))
    with pytest.raises(DecryptionError):
        broken_cipher.decrypt_text(wax_id, "test.purpose", blob)


def test_old_key_can_still_decrypt_after_rotation() -> None:
    """§8.3 item 13: a key rotation exercise can decrypt old records and
    write new records under the active key."""
    import uuid

    from waxprep.domain.identifiers import WaxId

    wax_id = WaxId(uuid.uuid4())
    key1 = b"k" * 32
    key2 = b"n" * 32
    old_cipher = AesGcmFieldCipher(KeyRing(keys={1: key1}, active_key_id=1))
    # Encrypt with old key
    blob = old_cipher.encrypt_text(wax_id, "test", "first message")
    # Rotate to key 2
    new_cipher = AesGcmFieldCipher(
        KeyRing(keys={1: key1, 2: key2}, active_key_id=2)
    )
    # Old record is still readable
    assert new_cipher.decrypt_text(wax_id, "test", blob) == "first message"
    # New write uses key 2
    new_blob = new_cipher.encrypt_text(wax_id, "test", "second message")
    assert new_blob[1] == 2, "new ciphertext must use the active key id"
    assert new_cipher.decrypt_text(wax_id, "test", new_blob) == "second message"
    # Old cipher cannot read new writes
    with pytest.raises(DecryptionError):
        old_cipher.decrypt_text(wax_id, "test", new_blob)
