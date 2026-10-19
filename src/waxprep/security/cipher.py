"""Field-level encryption for student data.

Every sensitive value is encrypted with AES-256-GCM using a key that is
derived separately for each student. The student's WAX ID and the field's
purpose are bound into the ciphertext, so a value copied into another
student's row (or another column) fails to decrypt.

Blind indexes let the database find exact words without ever seeing them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from waxprep.domain.identifiers import WaxId

FORMAT_VERSION = 1
NONCE_BYTES = 12
BLIND_INDEX_BYTES = 16
MASTER_KEY_BYTES = 32


class DecryptionError(RuntimeError):
    """Raised when stored data cannot be decrypted for this student/field."""


class FieldCipher(Protocol):
    """Encrypts and decrypts student fields; derives blind-index hashes."""

    def encrypt_bytes(self, wax_id: WaxId, purpose: str, plaintext: bytes) -> bytes: ...

    def decrypt_bytes(self, wax_id: WaxId, purpose: str, blob: bytes) -> bytes: ...

    def encrypt_text(self, wax_id: WaxId, purpose: str, plaintext: str) -> bytes: ...

    def decrypt_text(self, wax_id: WaxId, purpose: str, blob: bytes) -> str: ...

    def blind_index(self, wax_id: WaxId, token: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class KeyRing:
    """Master keys by numeric id. New data uses ``active_key_id``."""

    keys: Mapping[int, bytes]
    active_key_id: int
    blind_index_key_id: int = 1

    def __post_init__(self) -> None:
        if self.active_key_id not in self.keys:
            raise ValueError("active_key_id is not in the key ring")
        if self.blind_index_key_id not in self.keys:
            raise ValueError("blind_index_key_id is not in the key ring")
        for key_id, key in self.keys.items():
            if not 0 < key_id < 256:
                raise ValueError("key ids must be between 1 and 255")
            if len(key) != MASTER_KEY_BYTES:
                raise ValueError("master keys must be exactly 32 bytes")


def _hkdf(master: bytes, label: bytes, wax_id: WaxId) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=label + wax_id.bytes,
    ).derive(master)


def _aad(wax_id: WaxId, purpose: str) -> bytes:
    return wax_id.bytes + b"|" + purpose.encode("utf-8")


class AesGcmFieldCipher:
    """Production cipher: AES-256-GCM with per-student derived keys."""

    def __init__(self, ring: KeyRing) -> None:
        self._ring = ring
        self._enc_key = lru_cache(maxsize=4096)(self._derive_enc_key)
        self._blind_key = lru_cache(maxsize=4096)(self._derive_blind_key)

    def _derive_enc_key(self, key_id: int, wax_id: WaxId) -> bytes:
        return _hkdf(self._ring.keys[key_id], b"waxprep/enc/v1/", wax_id)

    def _derive_blind_key(self, wax_id: WaxId) -> bytes:
        master = self._ring.keys[self._ring.blind_index_key_id]
        return _hkdf(master, b"waxprep/blind/v1/", wax_id)

    def encrypt_bytes(self, wax_id: WaxId, purpose: str, plaintext: bytes) -> bytes:
        key_id = self._ring.active_key_id
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._enc_key(key_id, wax_id)).encrypt(
            nonce, plaintext, _aad(wax_id, purpose)
        )
        return bytes([FORMAT_VERSION, key_id]) + nonce + sealed

    def decrypt_bytes(self, wax_id: WaxId, purpose: str, blob: bytes) -> bytes:
        header = 2 + NONCE_BYTES
        if len(blob) <= header or blob[0] != FORMAT_VERSION:
            raise DecryptionError("unrecognised ciphertext format")
        key_id = blob[1]
        if key_id not in self._ring.keys:
            raise DecryptionError(f"unknown key id {key_id}")
        nonce = blob[2:header]
        try:
            return AESGCM(self._enc_key(key_id, wax_id)).decrypt(
                nonce, blob[header:], _aad(wax_id, purpose)
            )
        except InvalidTag as exc:
            raise DecryptionError("ciphertext failed authentication") from exc

    def encrypt_text(self, wax_id: WaxId, purpose: str, plaintext: str) -> bytes:
        return self.encrypt_bytes(wax_id, purpose, plaintext.encode("utf-8"))

    def decrypt_text(self, wax_id: WaxId, purpose: str, blob: bytes) -> str:
        return self.decrypt_bytes(wax_id, purpose, blob).decode("utf-8")

    def blind_index(self, wax_id: WaxId, token: str) -> bytes:
        digest = hmac.new(
            self._blind_key(wax_id), token.encode("utf-8"), hashlib.sha256
        ).digest()
        return digest[:BLIND_INDEX_BYTES]


def load_cipher_from_env(environ: Mapping[str, str] | None = None) -> AesGcmFieldCipher:
    """Build the cipher from environment variables.

    WAXPREP_MASTER_KEYS   "1:<base64 32 bytes>[,2:<base64 32 bytes>]"
    WAXPREP_ACTIVE_KEY_ID  id used for new data (default: highest id)
    """
    env = environ if environ is not None else os.environ
    raw = env.get("WAXPREP_MASTER_KEYS", "").strip()
    if not raw:
        raise RuntimeError("WAXPREP_MASTER_KEYS is not set")
    keys: dict[int, bytes] = {}
    for part in raw.split(","):
        key_id_text, _, encoded = part.strip().partition(":")
        keys[int(key_id_text)] = base64.b64decode(encoded, validate=True)
    active = int(env.get("WAXPREP_ACTIVE_KEY_ID", "") or max(keys))
    return AesGcmFieldCipher(KeyRing(keys=keys, active_key_id=active))
