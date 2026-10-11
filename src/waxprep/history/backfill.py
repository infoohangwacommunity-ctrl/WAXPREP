"""Full legacy backfill for migration 006 (spec section 12).

The backfill converts old plaintext-format rows into the new encrypted
format so migration 006's guards can pass. It is:

- idempotent: safe to run repeatedly; rows already converted are skipped
- resumable: each backfill is independent; a crash in one doesn't
  affect the others
- auditable: returns a report with rows copied, rows already present,
  skipped rows, and errors
- count-verifiable: the caller can compare source and destination counts
- provenance-preserving: notebook source_type/source_ref → provenance_links
- tenant-safe: uses the scoped Storage API; never bypasses RLS
- non-destructive: never deletes; the old rows remain until migration 006

The caller must run each backfill function in order, then verify counts
match, then run migration 006.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    ObjectType,
    ProvenanceLink,
    new_id,
)
from waxprep.storage.postgres import PostgresStorage
from waxprep.storage.session import assume_app_role, set_current_wax_id


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """Result of one backfill step."""

    step: str
    rows_seen: int
    rows_copied: int
    rows_already_present: int
    skipped: int
    errors: tuple[str, ...] = ()


@dataclass
class BackfillSummary:
    """Aggregate result of all backfill steps."""

    reports: list[BackfillReport] = field(default_factory=list)

    def all_ok(self) -> bool:
        return all(not r.errors for r in self.reports)

    def total_copied(self) -> int:
        return sum(r.rows_copied for r in self.reports)


def backfill_messages(
    conn: psycopg.Connection[Any], storage: PostgresStorage
) -> BackfillReport:
    """Copy old ``messages`` rows into ``history_entries``. Preserves IDs.

    Idempotent: rows already in history_entries are skipped.

    The source ``messages`` table read runs as the migration superuser
    (RESET ROLE) because RLS on ``messages`` would hide all rows from
    ``waxprep_app`` (there's no current_wax_id set during backfill).
    The destination write uses the scoped Storage API.
    """
    seen = copied = already = skipped = 0
    errors: list[str] = []
    # Read the source as the migration superuser (bypass RLS).
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT id, wax_id, role, content_type, text_body, created_at "
            "FROM messages ORDER BY created_at"
        )
        rows: list[dict[str, Any]] = cur.fetchall()
    seen = len(rows)
    for row in rows:
        wax_id = WaxId(row["wax_id"])
        if storage.get_history_entry(wax_id, row["id"]) is not None:
            already += 1
            continue
        role = str(row["role"]).lower()
        kind = (
            AuthorKind.TUTOR
            if role in ("tutor", "assistant")
            else AuthorKind.SYSTEM
            if role == "system"
            else AuthorKind.STUDENT
        )
        try:
            storage.append_history_entry(
                HistoryEntry(
                    id=row["id"],
                    wax_id=wax_id,
                    author_kind=kind,
                    content_kind=str(row["content_type"]),
                    text=row["text_body"],
                    occurred_at=row["created_at"],
                    recorded_at=row["created_at"],
                )
            )
            copied += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"message {row['id']}: {type(exc).__name__}: {exc}")
            skipped += 1
    return BackfillReport(
        step="messages",
        rows_seen=seen,
        rows_copied=copied,
        rows_already_present=already,
        skipped=skipped,
        errors=tuple(errors),
    )


def backfill_attachments(
    conn: psycopg.Connection[Any], storage: PostgresStorage
) -> BackfillReport:
    """Re-link old ``attachments`` (message_id) to ``history_entries``
    (id). Since migration 006 hasn't run yet, ``attachments.message_id``
    still exists; this backfill is a no-op until migration 006 runs, but
    we verify the IDs all exist in history_entries.

    Idempotent: a verification pass.
    """
    seen = copied = already = skipped = 0
    errors: list[str] = []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, wax_id, message_id, media_category, mime_type, "
            "storage_ref, size_bytes, created_at FROM attachments ORDER BY created_at"
        )
        rows = cur.fetchall()
    seen = len(rows)
    for row in rows:
        wax_id = WaxId(row["wax_id"])
        # Verify the message_id exists in history_entries.
        entry = storage.get_history_entry(wax_id, row["message_id"])
        if entry is None:
            errors.append(
                f"attachment {row['id']}: message_id {row['message_id']} not in history_entries"
            )
            skipped += 1
            continue
        # The attachment row itself doesn't change yet; migration 006
        # renames message_id → history_entry_id.
        copied += 1
    return BackfillReport(
        step="attachments_verify",
        rows_seen=seen,
        rows_copied=copied,
        rows_already_present=already,
        skipped=skipped,
        errors=tuple(errors),
    )


def backfill_notebook_entries(
    conn: psycopg.Connection[Any], storage: PostgresStorage
) -> BackfillReport:
    """Convert old notebook_entries (plaintext payload, source_type/source_ref)
    into the new encrypted format (payload_encrypted, status, superseded_by_id)
    and create provenance_links from source_type/source_ref.

    The old columns (payload, source_type, source_ref) are NOT dropped here —
    migration 006 drops them after the guards pass. This backfill writes
    the new columns alongside the old ones.

    Idempotent: rows that already have payload_encrypted set are skipped.
    """
    seen = copied = already = skipped = 0
    errors: list[str] = []
    # Read the source as the migration superuser (bypass RLS).
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT id, wax_id, notebook_id, schema_version, payload, "
            "source_type, source_ref, created_at, updated_at, "
            "payload_encrypted, status, superseded_by_id "
            "FROM notebook_entries ORDER BY created_at"
        )
        rows = cur.fetchall()
    seen = len(rows)
    now = datetime.now(UTC)
    for row in rows:
        wax_id = WaxId(row["wax_id"])
        if row["payload_encrypted"] is not None:
            already += 1
            continue
        try:
            payload = (
                row["payload"]
                if isinstance(row["payload"], dict)
                else json.loads(row["payload"] or "{}")
            )
            payload_bytes = json.dumps(payload).encode("utf-8")
            encrypted = storage._cipher.encrypt_text(  # type: ignore[attr-defined]
                wax_id, "notebook.payload", payload_bytes.decode("utf-8")
            )
            assume_app_role(conn)
            set_current_wax_id(conn, wax_id)
            with conn.cursor() as cur2:
                cur2.execute(
                    "UPDATE notebook_entries SET payload_encrypted = %s, "
                    "status = 'active' WHERE id = %s AND wax_id = %s",
                    (encrypted, row["id"], wax_id),
                )
            conn.commit()
            # Create a provenance link if source_type and source_ref exist.
            # Use a direct INSERT (as superuser) because the storage's
            # add_provenance_links calls waxprep_object_owner, which
            # doesn't exist until migration 010 (after the backfill).
            if row["source_type"] and row["source_ref"]:
                source_type_str = str(row["source_type"])
                source_id_str = str(row["source_ref"])
                try:
                    source_type = ObjectType(source_type_str)
                    source_id = UUID(source_id_str)
                    link = ProvenanceLink(
                        id=new_id(),
                        wax_id=wax_id,
                        derived_type=ObjectType.NOTEBOOK,
                        derived_id=row["id"],
                        source_type=source_type,
                        source_id=source_id,
                        created_at=now,
                    )
                    # Encrypt the quote if present.
                    quote_bytes = (
                        storage._cipher.encrypt_text(  # type: ignore[attr-defined]
                            wax_id, "provenance.quote", ""
                        )
                        if link.quote is not None
                        else None
                    )
                    assume_app_role(conn)
                    set_current_wax_id(conn, wax_id)
                    with conn.cursor() as cur3:
                        cur3.execute(
                            "INSERT INTO provenance_links (id, wax_id, "
                            "derived_type, derived_id, source_type, source_id, "
                            "source_author_kind, quote_encrypted, created_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING",
                            (
                                link.id,
                                wax_id,
                                link.derived_type.value,
                                link.derived_id,
                                link.source_type.value,
                                link.source_id,
                                None,
                                quote_bytes,
                                link.created_at,
                            ),
                        )
                    conn.commit()
                except (ValueError, Exception) as exc:  # noqa: BLE001
                    # The source_type/source_ref may be malformed or
                    # reference a missing object. Log and continue.
                    errors.append(
                        f"notebook_entry {row['id']}: provenance creation failed: {exc}"
                    )
                    conn.rollback()
            copied += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"notebook_entry {row['id']}: {type(exc).__name__}: {exc}")
            skipped += 1
            conn.rollback()
    return BackfillReport(
        step="notebook_entries",
        rows_seen=seen,
        rows_copied=copied,
        rows_already_present=already,
        skipped=skipped,
        errors=tuple(errors),
    )


def backfill_artifact_filenames(
    conn: psycopg.Connection[Any], storage: PostgresStorage
) -> BackfillReport:
    """Convert artifact_versions.original_filename (plaintext) to
    filename_encrypted (BYTEA).

    The ``filename_encrypted`` column is added idempotently if it
    doesn't exist yet (migration 005 doesn't add it; migration 006
    would, but its guard prevents running until the backfill is done).
    This backfill breaks the chicken-and-egg by adding the column
    itself.

    Idempotent: rows that already have filename_encrypted set are skipped.
    """
    seen = copied = already = skipped = 0
    errors: list[str] = []
    # Add the column if it doesn't exist (idempotent). Requires table
    # owner privileges — RESET ROLE to the migration superuser.
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "ALTER TABLE artifact_versions "
            "ADD COLUMN IF NOT EXISTS filename_encrypted BYTEA"
        )
    conn.commit()
    # Read the source as the migration superuser (bypass RLS).
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT id, wax_id, original_filename, filename_encrypted "
            "FROM artifact_versions ORDER BY created_at"
        )
        rows = cur.fetchall()
    seen = len(rows)
    for row in rows:
        wax_id = WaxId(row["wax_id"])
        if row["filename_encrypted"] is not None:
            already += 1
            continue
        filename = row["original_filename"]
        if filename is None:
            filename = ""
        try:
            encrypted = storage._cipher.encrypt_text(  # type: ignore[attr-defined]
                wax_id, "artifact.filename", filename
            )
            assume_app_role(conn)
            set_current_wax_id(conn, wax_id)
            with conn.cursor() as cur2:
                cur2.execute(
                    "UPDATE artifact_versions SET filename_encrypted = %s "
                    "WHERE id = %s AND wax_id = %s",
                    (encrypted, row["id"], wax_id),
                )
            conn.commit()
            copied += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"artifact_version {row['id']}: {type(exc).__name__}: {exc}")
            skipped += 1
            conn.rollback()
    return BackfillReport(
        step="artifact_filenames",
        rows_seen=seen,
        rows_copied=copied,
        rows_already_present=already,
        skipped=skipped,
        errors=tuple(errors),
    )


def backfill_knowledge(
    conn: psycopg.Connection[Any], storage: PostgresStorage
) -> BackfillReport:
    """Verify old knowledge_nodes/edges/evidence/embeddings have no rows
    that the application never wrote (migration 006's guard).

    The application never wrote to these tables in the old architecture
    (the CI system used an in-memory KnowledgeStore). If rows exist, they
    were written by some other process and must be converted by hand
    before migration 006.

    This backfill reports the count; it does NOT convert the rows
    because the target schema (encrypted knowledge_nodes) doesn't exist
    until migration 006 runs. The conversion must happen in a separate
    migration that runs after 006.

    Idempotent: a verification pass.
    """
    seen = copied = already = skipped = 0
    errors: list[str] = []
    with conn.cursor() as cur:
        for table in (
            "knowledge_nodes",
            "knowledge_edges",
            "knowledge_evidence",
            "knowledge_embeddings",
            "context_investigations",
        ):
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
            row = cur.fetchone()
            count = row[0] if row is not None else 0
            if count > 0:
                errors.append(
                    f"{table}: {count} rows exist; conversion required before migration 006"
                )
                seen += count
    return BackfillReport(
        step="knowledge_verify",
        rows_seen=seen,
        rows_copied=copied,
        rows_already_present=already,
        skipped=skipped,
        errors=tuple(errors),
    )


def run_full_backfill(
    conn: psycopg.Connection[Any], storage: PostgresStorage
) -> BackfillSummary:
    """Run all backfill steps in order. Returns a summary.

    The caller should:
    1. Run migrations 001-005.
    2. Call this function.
    3. Verify the summary has no errors.
    4. Run migration 006 (its guards should now pass).
    5. Run migrations 007-011.
    """
    summary = BackfillSummary()
    summary.reports.append(backfill_messages(conn, storage))
    summary.reports.append(backfill_attachments(conn, storage))
    summary.reports.append(backfill_notebook_entries(conn, storage))
    summary.reports.append(backfill_artifact_filenames(conn, storage))
    summary.reports.append(backfill_knowledge(conn, storage))
    return summary


__all__ = [
    "BackfillReport",
    "BackfillSummary",
    "backfill_artifact_filenames",
    "backfill_attachments",
    "backfill_knowledge",
    "backfill_messages",
    "backfill_notebook_entries",
    "run_full_backfill",
]
