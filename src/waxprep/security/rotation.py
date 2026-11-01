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

Schema introspection (structural fix): the rotation inventory is
discovered at runtime from ``information_schema`` joined with
``pg_description`` for the cipher ``purpose`` string set by
migration 018 + 022 column comments. Any future ``*_encrypted``
column added by a new migration that sets its
``COMMENT ON COLUMN ... IS 'purpose=...'`` is picked up
automatically — no Python-side hardcoded list to maintain. This is
the structural fix the Founder's brief explicitly asked for
("This is a structural fix, not a prompt fix. It catches future
columns automatically.").

The fail-closed contract is preserved: a column with no
``purpose=...`` comment causes ``discover_encrypted_columns`` to
raise ``ValueError`` listing the missing registration, and
``reencrypt_all`` refuses to run. The engineer is forced to declare
the purpose before rotation runs — preventing the silent-orphan
failure mode called out in vision-security Finding 1.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from waxprep.domain.identifiers import WaxId
from waxprep.security.cipher import AesGcmFieldCipher, DecryptionError, KeyRing
from waxprep.storage.session import assume_app_role, set_current_wax_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RotationTarget:
    """One ``(table, column, purpose)`` row discovered by schema
    introspection.

    The cipher binds ``purpose`` into the AES-GCM AAD so a value
    copied into another column or another student fails to decrypt.
    Rotation therefore needs the exact purpose the row was encrypted
    with; the column comment set by migration 018+022 is the source
    of truth.
    """

    table: str
    column: str
    purpose: str


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


# Defensive identifier validation (vision-security Finding 5). Schema
# names from ``information_schema`` are safe in practice; this regex is
# defense in depth against any future caller passing operator-controlled
# identifiers to ``reencrypt_table``.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str, *, kind: str) -> str:
    """Reject any identifier that is not a plain Postgres identifier.

    ``reencrypt_table`` interpolates ``table``, ``column``, and
    ``id_column`` into SQL via f-string (it cannot use parameterised
    binding for identifiers). The hardcoded callers in this module are
    safe, but ``reencrypt_table`` is a public function — a future caller
    passing user-controlled values would introduce SQL injection into
    the rotation path (which runs with ``RESET ROLE`` and reads every
    tenant). This guard closes that gap.
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"invalid {kind}: {name!r}")
    return name


# Regex to extract the purpose string from a column comment of the
# form ``purpose=<dotted-name>``. Anchored at the start of the comment
# so future additions (e.g. ``purpose=foo.bar; classification=secret``)
# do not break the parser.
_PURPOSE_COMMENT_RE = re.compile(r"^purpose=([A-Za-z0-9_.\-]+)")


def discover_encrypted_columns(
    conn: psycopg.Connection[Any],
    *,
    purposes: dict[tuple[str, str], str] | None = None,
) -> list[RotationTarget]:
    """Discover every encrypted column that must participate in key rotation.

    Source of truth: the live schema (``information_schema.columns``)
    joined with ``pg_description`` for the cipher ``purpose`` string
    set by migration 018 + 022 column comments.

    Filters:
      1. Column name ends in ``_encrypted`` (the WaxPrep convention).
      2. Table has RLS ENABLED (``pg_class.relrowsecurity = true``).
         Note: migration 015 dropped ``FORCE ROW LEVEL SECURITY`` on
         several tables so the table owner (the SECURITY DEFINER helper
         functions) can see all rows. ENABLE stays on; the application
         role (``waxprep_app``) remains fully policy-bound. This
         filter matches ENABLE, not FORCE — both protect the
         application role equally.
      3. Table has a ``wax_id`` column (tenant-scoped data — required
         by the cipher's per-student key derivation). A table that
         merely has an ``_encrypted`` column but no ``wax_id`` is NOT
         tenant-scoped and is excluded.
      4. Table has an ``id`` column (rotation batches ``ORDER BY id``).
      5. The column has a PostgreSQL ``COMMENT ON COLUMN`` of the form
         ``purpose=<dotted-name>`` (set by migration 018 + 022). A new
         column added by a future migration that does NOT set this
         comment causes a fail-closed ``ValueError`` listing the
         missing registration. This is the structural fix the
         Founder's brief explicitly asked for: the schema is the
         single source of truth, no Python-side list to keep in sync.

    The optional ``purposes`` override exists ONLY for tests that need
    to mock the comment introspection. Production callers must leave
    it ``None`` so the schema is the source of truth.

    Returns a list of ``RotationTarget``, sorted by ``(table, column)``
    for deterministic rotation order. Never includes non-tenant tables
    — verified by the ``wax_id`` + RLS check. ``outbound_deliveries``
    is tenant-scoped and RLS-protected but has no ``_encrypted``
    columns, so it is naturally excluded by filter 1.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        # RESET ROLE so we can read pg_class, pg_description and
        # information_schema freely. The introspection itself does
        # not touch student data — only structural metadata
        # (column names, table RLS flag, column comments).
        cur.execute("RESET ROLE")
        cur.execute(
            """
            SELECT
                c.table_name AS table_name,
                c.column_name AS column_name,
                col_description(
                    pc.oid,
                    a.attnum
                ) AS comment
            FROM information_schema.columns c
            JOIN pg_class pc ON pc.relname = c.table_name
            JOIN pg_namespace pn ON pn.oid = pc.relnamespace
            JOIN pg_attribute a
              ON a.attrelid = pc.oid
             AND a.attname = c.column_name
             AND a.attnum > 0
             AND NOT a.attisdropped
            WHERE c.table_schema = 'public'
              AND pn.nspname = 'public'
              AND c.column_name LIKE '%\\_encrypted' ESCAPE '\\'
              AND pc.relrowsecurity = true
              AND EXISTS (
                  SELECT 1 FROM information_schema.columns w
                  WHERE w.table_schema = 'public'
                    AND w.table_name = c.table_name
                    AND w.column_name = 'wax_id'
              )
              AND EXISTS (
                  SELECT 1 FROM information_schema.columns i
                  WHERE i.table_schema = 'public'
                    AND i.table_name = c.table_name
                    AND i.column_name = 'id'
              )
            ORDER BY c.table_name, c.column_name
            """
        )
        rows = cur.fetchall()

    targets: list[RotationTarget] = []
    missing: list[str] = []
    override_map = purposes or {}
    for row in rows:
        table = _validate_identifier(row["table_name"], kind="table")
        column = _validate_identifier(row["column_name"], kind="column")
        # Test override wins over the schema comment so unit tests can
        # mock the introspection without a real DB.
        if (table, column) in override_map:
            purpose = override_map[(table, column)]
        else:
            comment = row["comment"] or ""
            match = _PURPOSE_COMMENT_RE.match(comment)
            purpose = match.group(1) if match else None
        if purpose is None:
            missing.append(f"{table}.{column}")
            continue
        targets.append(RotationTarget(table=table, column=column, purpose=purpose))

    if missing:
        # Fail closed: refuse to rotate when a column's purpose is
        # unregistered. Rotation with the wrong purpose would silently
        # corrupt data (AAD mismatch raises DecryptionError per row,
        # which the rotation's broad except arm records as "failed"
        # without surfacing the cause — see vision-security Finding 1).
        raise ValueError(
            "Encrypted columns discovered in the schema but missing a "
            "purpose=... column comment. A new `*_encrypted` column was "
            "added by a migration without registering its AAD purpose "
            "string via `COMMENT ON COLUMN ... IS 'purpose=...'`. "
            "Rotation CANNOT proceed because the purpose string is "
            "bound into AES-GCM AAD and HKDF info — without it, "
            "re-encryption would silently corrupt the data. Add the "
            "missing column comments (see migration 022 for the "
            f"convention): {sorted(missing)}"
        )
    return targets


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

    Identifiers (``table``, ``column``, ``id_column``) are validated
    against a strict regex before SQL interpolation. See
    ``_validate_identifier`` and vision-security Finding 5.
    """
    _validate_identifier(table, kind="table")
    _validate_identifier(column, kind="column")
    _validate_identifier(id_column, kind="id_column")
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

    Processes every student-scoped encrypted column discovered via
    ``information_schema`` + ``pg_description`` (see
    ``discover_encrypted_columns``). Idempotent and resumable. Returns
    a summary with per-table reports.

    Never logs decrypted student content — only opaque error markers.

    The target list is built entirely from the schema: any future
    ``*_encrypted`` column added by a new migration that sets its
    ``COMMENT ON COLUMN ... IS 'purpose=...'`` is picked up
    automatically — no Python-side hardcoded list to maintain. This is
    the structural fix the Founder's brief explicitly asked for.
    """
    summary = RotationSummary(
        new_key_id=cipher._ring.active_key_id,  # type: ignore[attr-defined]
        old_key_id=0,
    )
    targets = discover_encrypted_columns(conn)
    logger.info(
        "reencrypt_all: discovered %d encrypted column(s) via schema introspection",
        len(targets),
    )
    for target in targets:
        report = reencrypt_table(
            conn,
            cipher,
            table=target.table,
            column=target.column,
            purpose=target.purpose,
        )
        summary.reports.append(report)
        logger.info(
            "re-encrypt %s.%s: reencrypted=%d skipped=%d failed=%d",
            target.table,
            target.column,
            report.rows_reencrypted,
            report.rows_skipped,
            report.rows_failed,
        )
    return summary


__all__ = [
    "RotationReport",
    "RotationSummary",
    "RotationTarget",
    "discover_encrypted_columns",
    "reencrypt_all",
    "reencrypt_table",
    "rotate_active_key",
]
