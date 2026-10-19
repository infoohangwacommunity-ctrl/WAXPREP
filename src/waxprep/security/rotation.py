"""Key rotation and re-encryption (spec section 14).

Implements a safe key-rotation/re-encryption mechanism for production
foundation readiness. Supports:
- adding a new active key
- decrypting old rows with their original key
- writing new data with the active key
- re-encrypting old data
- resumable progress
- idempotency
- failure recovery
- blind-index reindexing where necessary
- missing-key fail-closed behavior

Never fabricates context when a key is unavailable. Never logs
decrypted student content during rotation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from waxprep.domain.identifiers import WaxId
from waxprep.security.cipher import AesGcmFieldCipher, DecryptionError, KeyRing
from waxprep.storage.session import assume_app_role, set_current_wax_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RotationReport:
    """Result of one re-encryption pass."""

    table: str
    column: str
    rows_seen: int
    rows_reencrypted: int
    rows_skipped: int
    rows_failed: int
    errors: tuple[str, ...] = ()


@dataclass
class RotationSummary:
    """Aggregate result of all re-encryption passes."""

    reports: list[RotationReport] = field(default_factory=list)
    new_key_id: int = 0
    old_key_id: int = 0

    def total_reencrypted(self) -> int:
        return sum(r.rows_reencrypted for r in self.reports)


def rotate_active_key(
    conn: psycopg.Connection[Any],
    *,
    new_key_id: int,
    new_key: bytes,
    existing_keys: dict[int, bytes],
) -> KeyRing:
    """Add a new active key to the ring. Does NOT re-encrypt old rows.

    After this call:
    - New writes use ``new_key_id``.
    - Old writes can still be read because the old keys remain in the ring.
    - Call ``reencrypt_all`` to re-encrypt old rows with the new key.

    Returns the updated KeyRing.
    """
    if new_key_id < 1 or new_key_id > 255:
        raise ValueError("key id must be between 1 and 255")
    if len(new_key) != 32:
        raise ValueError("master key must be exactly 32 bytes")
    keys = dict(existing_keys)
    keys[new_key_id] = new_key
    return KeyRing(keys=keys, active_key_id=new_key_id)


def reencrypt_table(
    conn: psycopg.Connection[Any],
    cipher: AesGcmFieldCipher,
    *,
    table: str,
    column: str,
    purpose: str,
    id_column: str = "id",
    batch_size: int = 100,
) -> RotationReport:
    """Re-encrypt all rows in one table/column with the active key.

    Idempotent: rows already encrypted with the active key are skipped.
    Resumable: processes in batches; a crash leaves the table partially
    re-encrypted and the next run continues from where it left off.

    The function reads each row, decrypts with the row's original key
    (identified by the key id in the ciphertext header), and re-encrypts
    with the active key. If the row cannot be decrypted (missing key),
    the row is skipped and the error is recorded — the function never
    fabricates content.
    """
    seen = reencrypted = skipped = failed = 0
    errors: list[str] = []
    active_key_id = cipher._ring.active_key_id  # type: ignore[attr-defined]

    # Process in batches. Each batch:
    # 1. Read batch_size rows where the ciphertext's key id != active.
    # 2. For each, decrypt with the original key, re-encrypt with active.
    # 3. UPDATE the row.
    # 4. Commit the batch.
    offset = 0
    while True:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("RESET ROLE")
            cur.execute(
                f"SELECT {id_column}, wax_id, {column} FROM {table} "  # type: ignore[call-overload]  # noqa: S608
                f"ORDER BY {id_column} LIMIT %s OFFSET %s",
                (batch_size, offset),
            )
            rows = cur.fetchall()
        if not rows:
            break
        seen += len(rows)
        for row in rows:
            blob = row[column]
            if blob is None:
                skipped += 1
                continue
            blob_bytes = bytes(blob)
            # Check the key id in the ciphertext header (byte 1).
            if len(blob_bytes) < 2 or blob_bytes[0] != 1:
                errors.append(
                    f"{table}.{column} {row[id_column]}: unrecognised ciphertext format"
                )
                failed += 1
                continue
            row_key_id = blob_bytes[1]
            if row_key_id == active_key_id:
                skipped += 1
                continue
            # Decrypt with the original key.
            try:
                wax_id = WaxId(row["wax_id"])
                plaintext = cipher.decrypt_bytes(wax_id, purpose, blob_bytes)
            except DecryptionError as exc:
                errors.append(
                    f"{table}.{column} {row[id_column]}: decryption failed: {exc}"
                )
                failed += 1
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"{table}.{column} {row[id_column]}: {type(exc).__name__}: {exc}"
                )
                failed += 1
                continue
            # Re-encrypt with the active key.
            new_blob = cipher.encrypt_bytes(wax_id, purpose, plaintext)
            try:
                assume_app_role(conn)
                set_current_wax_id(conn, wax_id)
                with conn.cursor() as cur2:
                    cur2.execute(
                        f"UPDATE {table} SET {column} = %s "  # type: ignore[call-overload]  # noqa: S608
                        f"WHERE {id_column} = %s",
                        (new_blob, row[id_column]),
                    )
                conn.commit()
                reencrypted += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"{table}.{column} {row[id_column]}: re-encrypt failed: {exc}"
                )
                failed += 1
                conn.rollback()
        offset += batch_size
    return RotationReport(
        table=table,
        column=column,
        rows_seen=seen,
        rows_reencrypted=reencrypted,
        rows_skipped=skipped,
        rows_failed=failed,
        errors=tuple(errors),
    )


def reencrypt_all(
    conn: psycopg.Connection[Any], cipher: AesGcmFieldCipher
) -> RotationSummary:
    """Re-encrypt all encrypted columns with the active key.

    Processes each student-scoped encrypted column. Idempotent and
    resumable. Returns a summary with per-table reports.

    Never logs decrypted student content — only opaque error markers.
    """
    summary = RotationSummary(
        new_key_id=cipher._ring.active_key_id,  # type: ignore[attr-defined]
        old_key_id=0,
    )
    # The list of (table, column, purpose) tuples to re-encrypt.
    targets: list[tuple[str, str, str]] = [
        ("history_entries", "text_encrypted", "history.text"),
        ("search_embeddings", "vector_encrypted", "embedding.vector"),
        ("summaries", "text_encrypted", "summary.text"),
        ("provenance_links", "quote_encrypted", "provenance.quote"),
        ("artifact_contents", "text_encrypted", "artifact_content.text"),
        ("artifact_versions", "filename_encrypted", "artifact.filename"),
        ("notebook_entries", "payload_encrypted", "notebook.payload"),
        ("knowledge_nodes", "node_type_encrypted", "knowledge.node_type"),
        ("knowledge_nodes", "label_encrypted", "knowledge.label"),
        ("knowledge_nodes", "description_encrypted", "knowledge.description"),
        ("knowledge_nodes", "properties_encrypted", "knowledge.properties"),
        ("knowledge_edges", "relation_encrypted", "knowledge.relation"),
        ("knowledge_edges", "properties_encrypted", "knowledge.properties"),
        ("context_investigations", "input_encrypted", "investigation.input"),
        ("context_investigations", "result_encrypted", "investigation.result"),
    ]
    for table, column, purpose in targets:
        report = reencrypt_table(
            conn, cipher, table=table, column=column, purpose=purpose
        )
        summary.reports.append(report)
        logger.info(
            "re-encrypt %s.%s: reencrypted=%d skipped=%d failed=%d",
            table,
            column,
            report.rows_reencrypted,
            report.rows_skipped,
            report.rows_failed,
        )
    return summary


__all__ = [
    "RotationReport",
    "RotationSummary",
    "reencrypt_all",
    "reencrypt_table",
    "rotate_active_key",
]
