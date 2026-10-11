"""Encrypted private byte store.

Bytes are encrypted with the existing AesGcmFieldCipher (per-student keys).
Deletion removes ciphertext; success is only reported when gone.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from waxprep.domain.identifiers import WaxId, parse_wax_id
from waxprep.security.cipher import AesGcmFieldCipher

PURPOSE = "media_bytes"


class ByteStore(Protocol):
    def put(self, wax_id: WaxId, digest: str, plaintext: bytes) -> str:
        """Encrypt and store. Returns storage_ref."""
        ...

    def get(self, storage_ref: str) -> bytes:
        """Decrypt and return plaintext. Raises KeyError if missing."""
        ...

    def delete(self, storage_ref: str) -> None:
        """Idempotent delete of ciphertext."""
        ...

    def exists(self, storage_ref: str) -> bool: ...


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_ref(storage_ref: str) -> tuple[str, str, str]:
    """scheme://wax_id/digest"""
    scheme, _, rest = storage_ref.partition("://")
    wax_part, _, digest = rest.partition("/")
    if not scheme or not wax_part or not digest:
        raise ValueError("invalid storage_ref")
    return scheme, wax_part, digest


class InMemoryByteStore:
    """Test/dev store: ciphertext held in process memory."""

    def __init__(self, cipher: AesGcmFieldCipher) -> None:
        self._cipher = cipher
        self._blobs: dict[str, bytes] = {}

    def put(self, wax_id: WaxId, digest: str, plaintext: bytes) -> str:
        ref = f"mem://{wax_id}/{digest}"
        self._blobs[ref] = self._cipher.encrypt_bytes(wax_id, PURPOSE, plaintext)
        return ref

    def get(self, storage_ref: str) -> bytes:
        ct = self._blobs.get(storage_ref)
        if ct is None:
            raise KeyError(storage_ref)
        _, wax_s, _ = _parse_ref(storage_ref)
        return self._cipher.decrypt_bytes(parse_wax_id(wax_s), PURPOSE, ct)

    def delete(self, storage_ref: str) -> None:
        self._blobs.pop(storage_ref, None)

    def exists(self, storage_ref: str) -> bool:
        return storage_ref in self._blobs


class EncryptedFileByteStore:
    """Filesystem-backed encrypted store under a private root directory."""

    def __init__(self, cipher: AesGcmFieldCipher, root: str | Path) -> None:
        self._cipher = cipher
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        os.chmod(self._root, 0o700)

    def _path(self, storage_ref: str) -> Path:
        scheme, wax_s, digest = _parse_ref(storage_ref)
        if scheme != "file":
            raise ValueError("invalid storage_ref scheme")
        # digest is hex — safe path component
        rel = f"{wax_s}/{digest}.bin"
        path = (self._root / rel).resolve()
        if not str(path).startswith(str(self._root.resolve())):
            raise ValueError("path traversal rejected")
        return path

    def put(self, wax_id: WaxId, digest: str, plaintext: bytes) -> str:
        ref = f"file://{wax_id}/{digest}"
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._cipher.encrypt_bytes(wax_id, PURPOSE, plaintext))
        os.chmod(path, 0o600)
        return ref

    def get(self, storage_ref: str) -> bytes:
        path = self._path(storage_ref)
        if not path.is_file():
            raise KeyError(storage_ref)
        _, wax_s, _ = _parse_ref(storage_ref)
        return self._cipher.decrypt_bytes(parse_wax_id(wax_s), PURPOSE, path.read_bytes())

    def delete(self, storage_ref: str) -> None:
        path = self._path(storage_ref)
        if path.is_file():
            path.unlink()

    def exists(self, storage_ref: str) -> bool:
        try:
            return self._path(storage_ref).is_file()
        except ValueError:
            return False
