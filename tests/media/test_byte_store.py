"""Encrypted byte store security tests."""

from __future__ import annotations

import pytest

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.media.byte_store import (
    EncryptedFileByteStore,
    InMemoryByteStore,
    content_digest,
)
from waxprep.media.ingest import delete_attachment_bytes
from waxprep.media.limits import MediaLimitError, enforce_inbound_size
from waxprep.settings import MediaLimits


def test_inmemory_round_trip_and_delete(tmp_path) -> None:
    store = InMemoryByteStore(TEST_CIPHER)
    data = b"secret-student-bytes"
    digest = content_digest(data)
    ref = store.put(new_wax_id(), digest, data)
    assert store.get(ref) == data
    # ciphertext is not plaintext
    assert store._blobs[ref] != data
    store.delete(ref)
    assert not store.exists(ref)
    with pytest.raises(KeyError):
        store.get(ref)
    # idempotent delete
    store.delete(ref)


def test_file_store_path_traversal_rejected(tmp_path) -> None:
    store = EncryptedFileByteStore(TEST_CIPHER, tmp_path)
    with pytest.raises(ValueError):
        store.get("file://../../etc/passwd")


def test_file_store_round_trip(tmp_path) -> None:
    store = EncryptedFileByteStore(TEST_CIPHER, tmp_path)
    data = b"pdf-bytes"
    ref = store.put(new_wax_id(), content_digest(data), data)
    assert store.get(ref) == data
    delete_attachment_bytes(store, ref)
    assert not store.exists(ref)


def test_inbound_size_cap() -> None:
    limits = MediaLimits(max_inbound_bytes=10)
    enforce_inbound_size(5, limits)
    with pytest.raises(MediaLimitError) as ei:
        enforce_inbound_size(11, limits)
    assert ei.value.code == "inbound_too_large"
