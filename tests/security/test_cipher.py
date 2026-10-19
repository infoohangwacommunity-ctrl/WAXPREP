"""Field encryption: private per student, per field, and tamper-evident."""

from __future__ import annotations

import base64

import pytest

from waxprep.domain.identifiers import new_wax_id
from waxprep.security.cipher import (
    AesGcmFieldCipher,
    DecryptionError,
    KeyRing,
    load_cipher_from_env,
)

KEY1 = b"a" * 32
KEY2 = b"b" * 32


def _cipher() -> AesGcmFieldCipher:
    return AesGcmFieldCipher(KeyRing(keys={1: KEY1}, active_key_id=1))


def test_round_trip_and_ciphertext_hides_text() -> None:
    cipher, wax = _cipher(), new_wax_id()
    blob = cipher.encrypt_text(wax, "history.text", "I am always hungry after school")
    assert b"hungry" not in blob
    assert cipher.decrypt_text(wax, "history.text", blob) == (
        "I am always hungry after school"
    )


def test_same_text_encrypts_differently_each_time() -> None:
    cipher, wax = _cipher(), new_wax_id()
    assert cipher.encrypt_text(wax, "p", "same") != cipher.encrypt_text(
        wax, "p", "same"
    )


def test_ciphertext_cannot_move_to_another_student_or_field() -> None:
    cipher, a, b = _cipher(), new_wax_id(), new_wax_id()
    blob = cipher.encrypt_text(a, "history.text", "secret")
    with pytest.raises(DecryptionError):
        cipher.decrypt_text(b, "history.text", blob)
    with pytest.raises(DecryptionError):
        cipher.decrypt_text(a, "summary.text", blob)


def test_tampering_is_detected() -> None:
    cipher, wax = _cipher(), new_wax_id()
    blob = bytearray(cipher.encrypt_text(wax, "p", "secret"))
    blob[-1] ^= 1
    with pytest.raises(DecryptionError):
        cipher.decrypt_text(wax, "p", bytes(blob))


def test_blind_index_is_stable_per_student_and_different_across_students() -> None:
    cipher, a, b = _cipher(), new_wax_id(), new_wax_id()
    assert cipher.blind_index(a, "maths") == cipher.blind_index(a, "maths")
    assert cipher.blind_index(a, "maths") != cipher.blind_index(b, "maths")
    assert cipher.blind_index(a, "maths") != cipher.blind_index(a, "physics")


def test_key_rotation_old_data_still_readable() -> None:
    wax = new_wax_id()
    old = AesGcmFieldCipher(KeyRing(keys={1: KEY1}, active_key_id=1))
    blob = old.encrypt_text(wax, "p", "written before rotation")
    rotated = AesGcmFieldCipher(KeyRing(keys={1: KEY1, 2: KEY2}, active_key_id=2))
    assert rotated.decrypt_text(wax, "p", blob) == "written before rotation"
    assert rotated.encrypt_text(wax, "p", "x")[1] == 2


def test_key_ring_rejects_short_keys() -> None:
    with pytest.raises(ValueError):
        KeyRing(keys={1: b"short"}, active_key_id=1)


def test_load_from_environment() -> None:
    encoded = base64.b64encode(KEY1).decode()
    cipher = load_cipher_from_env({"WAXPREP_MASTER_KEYS": f"1:{encoded}"})
    wax = new_wax_id()
    assert cipher.decrypt_text(wax, "p", cipher.encrypt_text(wax, "p", "hi")) == "hi"
    with pytest.raises(RuntimeError):
        load_cipher_from_env({})
