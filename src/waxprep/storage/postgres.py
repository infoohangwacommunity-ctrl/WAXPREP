"""PostgreSQL storage implementation.

Every student-scoped call sets the request's WAX ID so Row-Level Security
applies. Sensitive text is encrypted by the application before it reaches
the database and decrypted on the way out.
"""

from __future__ import annotations

import contextlib
import json
import struct
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactContent,
    ArtifactReference,
    ArtifactStatus,
    ArtifactVersion,
    Attachment,
    AuthorKind,
    ChannelIdentity,
    DeletionRequest,
    DeletionRequestStatus,
    DerivationJob,
    EmbeddingRecord,
    Event,
    HistoryEntry,
    InteractiveOffer,
    InteractiveOfferStatus,
    InvestigationRecord,
    JobStatus,
    Notebook,
    NotebookEntry,
    NotebookEntryStatus,
    ObjectType,
    OutboundDelivery,
    OutboundStatus,
    ProvenanceLink,
    Student,
    StudentStatus,
    Summary,
    SummaryStatus,
    Workspace,
    new_id,
)
from waxprep.knowledge.models import KnowledgeEdge, KnowledgeNode, KnowledgeNodeStatus
from waxprep.security.cipher import FieldCipher
from waxprep.storage.protocol import TokenMatch, TokenStats
from waxprep.storage.session import assume_app_role, set_current_wax_id

Row = dict[str, Any]

HISTORY_COLUMNS = (
    "id, wax_id, author_kind, content_kind, text_encrypted, occurred_at, "
    "recorded_at, channel, channel_ref, thread_ref, reply_to_id"
)
NOTEBOOK_COLUMNS = (
    "id, wax_id, notebook_id, schema_version, payload_encrypted, status, "
    "superseded_by_id, created_at, updated_at"
)
VERSION_COLUMNS = (
    "id, wax_id, artifact_id, version_number, storage_ref, filename_encrypted, "
    "mime_type, size_bytes, checksum_sha256, created_at"
)
ARTIFACT_COLUMNS = (
    "id, wax_id, workspace_id, status, current_version_id, created_at, updated_at"
)
NODE_COLUMNS = (
    "id, wax_id, node_type_encrypted, label_encrypted, description_encrypted, "
    "properties_encrypted, status, created_at, updated_at"
)
EDGE_COLUMNS = (
    "id, wax_id, source_node_id, target_node_id, relation_encrypted, "
    "properties_encrypted, valid_from, valid_until, created_at"
)
JOB_COLUMNS = (
    "id, wax_id, kind, object_type, object_id, status, attempts, last_error, "
    "run_after, created_at, updated_at, lease_until"
)
SUMMARY_COLUMNS = (
    "id, wax_id, text_encrypted, covers_from, covers_until, level, status, "
    "produced_by, created_at"
)
LINK_COLUMNS = (
    "id, wax_id, derived_type, derived_id, source_type, source_id, "
    "source_author_kind, quote_encrypted, created_at"
)


def _pack_vector(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(raw: bytes) -> tuple[float, ...]:
    return tuple(struct.unpack(f"<{len(raw) // 4}f", raw))


class PostgresStorage:
    """Ownership-scoped, encrypted PostgreSQL storage."""

    def __init__(self, conn: psycopg.Connection[Any], cipher: FieldCipher) -> None:
        self._conn = conn
        self._cipher = cipher
        self._txn_depth = 0
        assume_app_role(conn)

    # --- helpers -----------------------------------------------------------
    def _write(self, wax_id: WaxId | None, sql: str, params: tuple[Any, ...]) -> int:
        if wax_id is not None:
            set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)  # pyright: ignore[reportArgumentType]
            count = cur.rowcount
        if self._txn_depth == 0:
            self._conn.commit()
        return count

    def _write_many(self, wax_id: WaxId, sql: str, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor() as cur:
            cur.executemany(sql, rows)  # pyright: ignore[reportArgumentType]
        if self._txn_depth == 0:
            self._conn.commit()

    def _read(self, wax_id: WaxId, sql: str, params: tuple[Any, ...]) -> list[Row]:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # pyright: ignore[reportArgumentType]
            rows: list[Row] = cur.fetchall()
        if self._txn_depth == 0:
            self._conn.commit()
        return rows

    @contextlib.contextmanager
    def transaction(self) -> Iterator[PostgresStorage]:
        """Wrap multiple storage operations in one DB transaction.

        If any operation within the context raises, the whole transaction
        rolls back — no partial state. Used by EvolutionService to make
        derived-object + provenance + index writes atomic (spec section 9).
        """
        self._txn_depth += 1
        try:
            yield self
            if self._txn_depth == 1:
                self._conn.commit()
        except Exception:
            if self._txn_depth == 1:
                self._conn.rollback()
            raise
        finally:
            self._txn_depth -= 1

    def _one(self, wax_id: WaxId, sql: str, params: tuple[Any, ...]) -> Row | None:
        rows = self._read(wax_id, sql, params)
        return rows[0] if rows else None

    def _enc(self, wax_id: WaxId, purpose: str, text: str) -> bytes:
        return self._cipher.encrypt_text(wax_id, purpose, text)

    def _dec(self, wax_id: WaxId, purpose: str, blob: Any) -> str:
        return self._cipher.decrypt_text(wax_id, purpose, bytes(blob))

    def _enc_json(self, wax_id: WaxId, purpose: str, value: dict[str, Any]) -> bytes:
        return self._enc(wax_id, purpose, json.dumps(value))

    def _dec_json(self, wax_id: WaxId, purpose: str, blob: Any) -> dict[str, Any]:
        loaded = json.loads(self._dec(wax_id, purpose, blob))
        return dict(loaded)

    # --- identity -------------------------------------------------------------
    def create_student(self, student: Student) -> Student:
        self._write(
            student.wax_id,
            "INSERT INTO students (wax_id, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (
                student.wax_id,
                student.status.value,
                student.created_at,
                student.updated_at,
            ),
        )
        return student

    def get_student(self, wax_id: WaxId) -> Student | None:
        row = self._one(
            wax_id,
            "SELECT wax_id, status, created_at, updated_at FROM students WHERE wax_id = %s",
            (wax_id,),
        )
        if row is None:
            return None
        return Student(
            wax_id=WaxId(row["wax_id"]),
            status=StudentStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_channel_identity(self, identity: ChannelIdentity) -> ChannelIdentity:
        self._write(
            identity.wax_id,
            "INSERT INTO channel_identities (id, wax_id, channel, external_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                identity.id,
                identity.wax_id,
                identity.channel,
                identity.external_id,
                identity.created_at,
            ),
        )
        return identity

    def list_channel_identities_for_wax(
        self, wax_id: WaxId
    ) -> tuple[ChannelIdentity, ...]:
        rows = self._read(
            wax_id,
            "SELECT id, wax_id, channel, external_id, created_at "
            "FROM channel_identities WHERE wax_id = %s",
            (wax_id,),
        )
        return tuple(
            ChannelIdentity(
                id=r["id"],
                wax_id=WaxId(r["wax_id"]),
                channel=r["channel"],
                external_id=r["external_id"],
                created_at=r["created_at"],
            )
            for r in rows
        )

    def get_channel_identity(
        self, channel: str, external_id: str
    ) -> ChannelIdentity | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, wax_id, channel, external_id, created_at "
                "FROM waxprep_lookup_channel(%s, %s)",
                (channel, external_id),
            )
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            return None
        return ChannelIdentity(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            channel=row["channel"],
            external_id=row["external_id"],
            created_at=row["created_at"],
        )

    # --- unified history ----------------------------------------------------------
    def _history(self, row: Row) -> HistoryEntry:
        # Support dict rows (preferred) and plain tuples (defensive).
        if not isinstance(row, dict):
            cols = [c.strip() for c in HISTORY_COLUMNS.split(",")]
            row = dict(zip(cols, row, strict=False))  # type: ignore[arg-type]
        wax_id = WaxId(row["wax_id"])
        blob = row["text_encrypted"]
        return HistoryEntry(
            id=row["id"],
            wax_id=wax_id,
            author_kind=AuthorKind(row["author_kind"]),
            content_kind=row["content_kind"],
            text=self._dec(wax_id, "history.text", blob) if blob is not None else None,
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
            channel=row["channel"],
            channel_ref=row["channel_ref"],
            thread_ref=row["thread_ref"],
            reply_to_id=row["reply_to_id"],
        )

    def append_history_entry(self, entry: HistoryEntry) -> HistoryEntry:
        blob = (
            self._enc(entry.wax_id, "history.text", entry.text)
            if entry.text is not None
            else None
        )
        inserted = self._write(
            entry.wax_id,
            f"INSERT INTO history_entries ({HISTORY_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                entry.id,
                entry.wax_id,
                entry.author_kind.value,
                entry.content_kind,
                blob,
                entry.occurred_at,
                entry.recorded_at,
                entry.channel,
                entry.channel_ref,
                entry.thread_ref,
                entry.reply_to_id,
            ),
        )
        if inserted:
            return entry
        if entry.channel is not None and entry.channel_ref is not None:
            row = self._one(
                entry.wax_id,
                f"SELECT {HISTORY_COLUMNS} FROM history_entries "
                "WHERE wax_id = %s AND channel = %s AND channel_ref = %s",
                (entry.wax_id, entry.channel, entry.channel_ref),
            )
            if row is not None:
                return self._history(row)
        raise ValueError("history entry already exists")

    def append_history_entry_with_jobs(
        self,
        entry: HistoryEntry,
        token_hashes: tuple[bytes, ...],
        jobs: tuple[DerivationJob, ...],
    ) -> tuple[HistoryEntry, bool]:
        """Atomic history + tokens + jobs write (Part 8 Blocker C).

        If anything in this transaction fails, nothing is committed and the
        channel adapter must allow a provider retry.
        """
        wax_id = entry.wax_id
        set_current_wax_id(self._conn, wax_id)
        blob = (
            self._enc(wax_id, "history.text", entry.text)
            if entry.text is not None
            else None
        )
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO history_entries ({HISTORY_COLUMNS}) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    entry.id,
                    wax_id,
                    entry.author_kind.value,
                    entry.content_kind,
                    blob,
                    entry.occurred_at,
                    entry.recorded_at,
                    entry.channel,
                    entry.channel_ref,
                    entry.thread_ref,
                    entry.reply_to_id,
                ),
            )
            inserted = cur.rowcount > 0
            if not inserted:
                # Duplicate (channel, channel_ref) delivery: load the
                # existing entry and return without enqueueing jobs.
                if entry.channel is not None and entry.channel_ref is not None:
                    cur.execute(
                        f"SELECT {HISTORY_COLUMNS} FROM history_entries "
                        "WHERE wax_id = %s AND channel = %s AND channel_ref = %s",
                        (wax_id, entry.channel, entry.channel_ref),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        self._conn.commit()
                        return self._history(row), False
                # Tutor one-reply-per-trigger unique index: concurrent turn may
                # already have saved the tutor reply. Resolve by reply_to_id.
                if (
                    entry.author_kind is AuthorKind.TUTOR
                    and entry.reply_to_id is not None
                ):
                    cur.execute(
                        f"SELECT {HISTORY_COLUMNS} FROM history_entries "
                        "WHERE wax_id = %s AND reply_to_id = %s "
                        "AND author_kind = %s",
                        (wax_id, entry.reply_to_id, AuthorKind.TUTOR.value),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        self._conn.commit()
                        return self._history(row), False
                self._conn.commit()
                raise ValueError("history entry already exists")
            # Insert succeeded: write tokens and jobs in the same transaction.
            if token_hashes:
                cur.execute(
                    "DELETE FROM search_tokens WHERE wax_id = %s "
                    "AND object_type = %s AND object_id = %s",
                    (wax_id, ObjectType.HISTORY.value, entry.id),
                )
                cur.executemany(
                    "INSERT INTO search_tokens "
                    "(wax_id, object_type, object_id, object_time, token_hash) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    [
                        (
                            wax_id,
                            ObjectType.HISTORY.value,
                            entry.id,
                            entry.occurred_at,
                            h,
                        )
                        for h in dict.fromkeys(token_hashes)
                    ],
                )
            if jobs:
                cur.executemany(
                    f"INSERT INTO derivation_jobs ({JOB_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    [
                        (
                            j.id,
                            j.wax_id,
                            j.kind,
                            j.object_type.value,
                            j.object_id,
                            j.status.value,
                            j.attempts,
                            j.last_error,
                            j.run_after,
                            j.created_at,
                            j.updated_at,
                            j.lease_until,
                        )
                        for j in jobs
                    ],
                )
        self._conn.commit()
        return entry, True

    def get_history_entry(self, wax_id: WaxId, entry_id: UUID) -> HistoryEntry | None:
        row = self._one(
            wax_id,
            f"SELECT {HISTORY_COLUMNS} FROM history_entries WHERE wax_id = %s AND id = %s",
            (wax_id, entry_id),
        )
        return self._history(row) if row else None

    def list_history(
        self,
        wax_id: WaxId,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> tuple[HistoryEntry, ...]:
        direction = "DESC" if newest_first else "ASC"
        rows = self._read(
            wax_id,
            f"SELECT {HISTORY_COLUMNS} FROM history_entries WHERE wax_id = %s "
            "AND (%s::timestamptz IS NULL OR occurred_at >= %s) "
            "AND (%s::timestamptz IS NULL OR occurred_at < %s) "
            f"ORDER BY occurred_at {direction}, seq {direction} "
            "LIMIT %s",
            (wax_id, since, since, until, until, limit),
        )
        return tuple(self._history(r) for r in rows)

    def find_history_by_channel_ref(
        self, wax_id: WaxId, channel: str, channel_ref: str
    ) -> HistoryEntry | None:
        rows = self._read(
            wax_id,
            f"SELECT {HISTORY_COLUMNS} FROM history_entries "
            "WHERE wax_id = %s AND channel = %s AND channel_ref = %s "
            "LIMIT 1",
            (wax_id, channel, channel_ref),
        )
        if not rows:
            return None
        return self._history(rows[0])

    def list_history_around(
        self, wax_id: WaxId, entry_id: UUID, *, before: int, after: int
    ) -> tuple[HistoryEntry, ...]:
        target = self.get_history_entry(wax_id, entry_id)
        if target is None:
            return ()
        anchor = (
            "(SELECT occurred_at, seq FROM history_entries "
            "WHERE wax_id = %s AND id = %s)"
        )
        earlier = self._read(
            wax_id,
            f"SELECT {HISTORY_COLUMNS} FROM history_entries WHERE wax_id = %s "
            f"AND (occurred_at, seq) < {anchor} "
            "ORDER BY occurred_at DESC, seq DESC LIMIT %s",
            (wax_id, wax_id, entry_id, before),
        )
        later = self._read(
            wax_id,
            f"SELECT {HISTORY_COLUMNS} FROM history_entries WHERE wax_id = %s "
            f"AND (occurred_at, seq) > {anchor} "
            "ORDER BY occurred_at, seq LIMIT %s",
            (wax_id, wax_id, entry_id, after),
        )
        return (
            tuple(self._history(r) for r in reversed(earlier))
            + (target,)
            + tuple(self._history(r) for r in later)
        )

    def list_unsummarized_history(
        self, wax_id: WaxId, *, before: datetime, limit: int
    ) -> tuple[HistoryEntry, ...]:
        rows = self._read(
            wax_id,
            f"SELECT {HISTORY_COLUMNS} FROM history_entries h "
            "WHERE h.wax_id = %s AND h.occurred_at < %s AND NOT EXISTS ("
            " SELECT 1 FROM provenance_links p JOIN summaries s "
            "  ON s.id = p.derived_id AND s.wax_id = p.wax_id "
            " WHERE p.wax_id = h.wax_id AND p.derived_type = 'summary' "
            "  AND p.source_type = 'history' AND p.source_id = h.id) "
            "ORDER BY h.occurred_at, h.seq LIMIT %s",
            (wax_id, before, limit),
        )
        return tuple(self._history(r) for r in rows)

    # --- attachments ----------------------------------------------------------------
    def create_attachment(self, attachment: Attachment) -> Attachment:
        self._write(
            attachment.wax_id,
            "INSERT INTO attachments (id, wax_id, history_entry_id, media_category, "
            "mime_type, storage_ref, size_bytes, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                attachment.id,
                attachment.wax_id,
                attachment.history_entry_id,
                attachment.media_category,
                attachment.mime_type,
                attachment.storage_ref,
                attachment.size_bytes,
                attachment.created_at,
            ),
        )
        return attachment

    @staticmethod
    def _attachment(row: Row) -> Attachment:
        return Attachment(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            history_entry_id=row["history_entry_id"],
            media_category=row["media_category"],
            mime_type=row["mime_type"],
            storage_ref=row["storage_ref"],
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
        )

    def get_attachment(self, wax_id: WaxId, attachment_id: UUID) -> Attachment | None:
        row = self._one(
            wax_id,
            "SELECT id, wax_id, history_entry_id, media_category, mime_type, "
            "storage_ref, size_bytes, created_at FROM attachments "
            "WHERE wax_id = %s AND id = %s",
            (wax_id, attachment_id),
        )
        return self._attachment(row) if row else None

    def list_attachments_for_entry(
        self, wax_id: WaxId, entry_id: UUID
    ) -> tuple[Attachment, ...]:
        rows = self._read(
            wax_id,
            "SELECT id, wax_id, history_entry_id, media_category, mime_type, "
            "storage_ref, size_bytes, created_at FROM attachments "
            "WHERE wax_id = %s AND history_entry_id = %s ORDER BY created_at",
            (wax_id, entry_id),
        )
        return tuple(self._attachment(r) for r in rows)

    # --- search index ---------------------------------------------------------------------
    def put_search_tokens(
        self,
        wax_id: WaxId,
        object_type: ObjectType,
        object_id: UUID,
        object_time: datetime,
        token_hashes: tuple[bytes, ...],
    ) -> None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM search_tokens WHERE wax_id = %s AND object_type = %s "
                "AND object_id = %s",
                (wax_id, object_type.value, object_id),
            )
            cur.executemany(
                "INSERT INTO search_tokens (wax_id, object_type, object_id, "
                "object_time, token_hash) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                [
                    (wax_id, object_type.value, object_id, object_time, h)
                    for h in dict.fromkeys(token_hashes)
                ],
            )
        self._conn.commit()

    def token_stats(
        self,
        wax_id: WaxId,
        token_hashes: tuple[bytes, ...],
        object_types: tuple[ObjectType, ...],
    ) -> TokenStats:
        types = [t.value for t in object_types]
        total_row = self._one(
            wax_id,
            "SELECT count(*) AS n FROM (SELECT DISTINCT object_type, object_id "
            "FROM search_tokens WHERE wax_id = %s AND object_type = ANY(%s::text[])) d",
            (wax_id, types),
        )
        frequency: dict[bytes, int] = {}
        if token_hashes:
            for row in self._read(
                wax_id,
                "SELECT token_hash, count(*) AS n FROM search_tokens "
                "WHERE wax_id = %s AND object_type = ANY(%s::text[]) "
                "AND token_hash = ANY(%s::bytea[]) GROUP BY token_hash",
                (wax_id, types, list(token_hashes)),
            ):
                frequency[bytes(row["token_hash"])] = int(row["n"])
        return TokenStats(
            total_objects=int(total_row["n"]) if total_row else 0,
            document_frequency=frequency,
        )

    def find_by_tokens(
        self,
        wax_id: WaxId,
        token_hashes: tuple[bytes, ...],
        *,
        object_types: tuple[ObjectType, ...],
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> tuple[TokenMatch, ...]:
        if not token_hashes:
            return ()
        # Blocker H: deterministic ORDER BY so the limit is reproducible.
        # ``limit=None`` searches the whole corpus — no artificial wall.
        sql_limit = "" if limit is None else " LIMIT %s"
        params: list[Any] = [
            wax_id,
            list(token_hashes),
            [t.value for t in object_types],
            since,
            since,
            until,
            until,
        ]
        if limit is not None:
            params.append(limit)
        rows = self._read(
            wax_id,
            "SELECT object_type, object_id, object_time, token_hash FROM search_tokens "
            "WHERE wax_id = %s AND token_hash = ANY(%s::bytea[]) "
            "AND object_type = ANY(%s::text[]) "
            "AND (%s::timestamptz IS NULL OR object_time >= %s) "
            "AND (%s::timestamptz IS NULL OR object_time < %s) "
            "ORDER BY object_time DESC, object_id, token_hash" + sql_limit,
            tuple(params),
        )
        return tuple(
            TokenMatch(
                ObjectType(r["object_type"]),
                r["object_id"],
                r["object_time"],
                bytes(r["token_hash"]),
            )
            for r in rows
        )

    def find_by_tokens_page(
        self,
        wax_id: WaxId,
        token_hashes: tuple[bytes, ...],
        *,
        object_types: tuple[ObjectType, ...],
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        cursor: tuple[datetime, UUID, bytes] | None = None,
    ) -> tuple[TokenMatch, ...]:
        """One page of token matches in deterministic order."""
        if not token_hashes:
            return ()
        if cursor is None:
            sql = (
                "SELECT object_type, object_id, object_time, token_hash FROM search_tokens "
                "WHERE wax_id = %s AND token_hash = ANY(%s::bytea[]) "
                "AND object_type = ANY(%s::text[]) "
                "AND (%s::timestamptz IS NULL OR object_time >= %s) "
                "AND (%s::timestamptz IS NULL OR object_time < %s) "
                "ORDER BY object_time DESC, object_id, token_hash LIMIT %s"
            )
            params: tuple[Any, ...] = (
                wax_id,
                list(token_hashes),
                [t.value for t in object_types],
                since,
                since,
                until,
                until,
                page_size,
            )
        else:
            cur_time, cur_id, cur_hash = cursor
            sql = (
                "SELECT object_type, object_id, object_time, token_hash FROM search_tokens "
                "WHERE wax_id = %s AND token_hash = ANY(%s::bytea[]) "
                "AND object_type = ANY(%s::text[]) "
                "AND (%s::timestamptz IS NULL OR object_time >= %s) "
                "AND (%s::timestamptz IS NULL OR object_time < %s) "
                "AND (object_time, object_id, token_hash) < (%s, %s, %s) "
                "ORDER BY object_time DESC, object_id, token_hash LIMIT %s"
            )
            params = (
                wax_id,
                list(token_hashes),
                [t.value for t in object_types],
                since,
                since,
                until,
                until,
                cur_time,
                cur_id,
                cur_hash,
                page_size,
            )
        rows = self._read(wax_id, sql, params)
        return tuple(
            TokenMatch(
                ObjectType(r["object_type"]),
                r["object_id"],
                r["object_time"],
                bytes(r["token_hash"]),
            )
            for r in rows
        )

    def put_embedding(self, record: EmbeddingRecord) -> None:
        blob = self._cipher.encrypt_bytes(
            record.wax_id, "embedding.vector", _pack_vector(record.vector)
        )
        self._write(
            record.wax_id,
            "INSERT INTO search_embeddings (id, wax_id, object_type, object_id, "
            "object_time, model, vector_encrypted, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (wax_id, object_type, object_id, model) DO UPDATE SET "
            "vector_encrypted = EXCLUDED.vector_encrypted, "
            "object_time = EXCLUDED.object_time, created_at = EXCLUDED.created_at",
            (
                record.id,
                record.wax_id,
                record.object_type.value,
                record.object_id,
                record.object_time,
                record.model,
                blob,
                record.created_at,
            ),
        )

    def delete_search_tokens(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> int:
        """Drop all search-token rows for one object (invalidation/deletion).

        Returns the number of rows dropped. Tokens are an index — removing
        them changes findability, not the underlying record. Encrypted text
        is never decrypted here.
        """
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM search_tokens WHERE wax_id = %s "
                "AND object_type = %s AND object_id = %s",
                (wax_id, object_type.value, object_id),
            )
            # rowcount is the number of rows deleted by the statement.
            return int(cur.rowcount or 0)

    def delete_embeddings(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> int:
        """Drop all embedding rows for one object (invalidation/deletion).

        Returns the number of rows dropped. The encrypted vector blobs are
        removed entirely — no plaintext is read or written here.
        """
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM search_embeddings WHERE wax_id = %s "
                "AND object_type = %s AND object_id = %s",
                (wax_id, object_type.value, object_id),
            )
            return int(cur.rowcount or 0)

    def list_embeddings(
        self,
        wax_id: WaxId,
        object_types: tuple[ObjectType, ...],
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> tuple[EmbeddingRecord, ...]:
        sql_limit = "" if limit is None else " LIMIT %s"
        params: list[Any] = [
            wax_id,
            [t.value for t in object_types],
            since,
            since,
            until,
            until,
        ]
        if limit is not None:
            params.append(limit)
        rows = self._read(
            wax_id,
            "SELECT id, wax_id, object_type, object_id, object_time, model, "
            "vector_encrypted, created_at FROM search_embeddings "
            "WHERE wax_id = %s AND object_type = ANY(%s::text[]) "
            "AND (%s::timestamptz IS NULL OR object_time >= %s) "
            "AND (%s::timestamptz IS NULL OR object_time < %s) "
            "ORDER BY object_time DESC, object_id" + sql_limit,
            tuple(params),
        )
        return tuple(self._embedding_record(r) for r in rows)

    def list_embeddings_page(
        self,
        wax_id: WaxId,
        object_types: tuple[ObjectType, ...],
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[EmbeddingRecord, ...]:
        if cursor is None:
            sql = (
                "SELECT id, wax_id, object_type, object_id, object_time, model, "
                "vector_encrypted, created_at FROM search_embeddings "
                "WHERE wax_id = %s AND object_type = ANY(%s::text[]) "
                "AND (%s::timestamptz IS NULL OR object_time >= %s) "
                "AND (%s::timestamptz IS NULL OR object_time < %s) "
                "ORDER BY object_time DESC, object_id LIMIT %s"
            )
            params: tuple[Any, ...] = (
                wax_id,
                [t.value for t in object_types],
                since,
                since,
                until,
                until,
                page_size,
            )
        else:
            cur_time, cur_id = cursor
            sql = (
                "SELECT id, wax_id, object_type, object_id, object_time, model, "
                "vector_encrypted, created_at FROM search_embeddings "
                "WHERE wax_id = %s AND object_type = ANY(%s::text[]) "
                "AND (%s::timestamptz IS NULL OR object_time >= %s) "
                "AND (%s::timestamptz IS NULL OR object_time < %s) "
                "AND (object_time, object_id) < (%s, %s) "
                "ORDER BY object_time DESC, object_id LIMIT %s"
            )
            params = (
                wax_id,
                [t.value for t in object_types],
                since,
                since,
                until,
                until,
                cur_time,
                cur_id,
                page_size,
            )
        rows = self._read(wax_id, sql, params)
        return tuple(self._embedding_record(r) for r in rows)

    def _embedding_record(self, r: Row) -> EmbeddingRecord:
        return EmbeddingRecord(
            id=r["id"],
            wax_id=WaxId(r["wax_id"]),
            object_type=ObjectType(r["object_type"]),
            object_id=r["object_id"],
            object_time=r["object_time"],
            model=r["model"],
            vector=_unpack_vector(
                self._cipher.decrypt_bytes(
                    WaxId(r["wax_id"]),
                    "embedding.vector",
                    bytes(r["vector_encrypted"]),
                )
            ),
            created_at=r["created_at"],
        )

    # --- notebook ---------------------------------------------------------------------------
    def get_or_create_notebook(self, wax_id: WaxId, now: datetime) -> Notebook:
        existing = self.get_notebook(wax_id)
        if existing is not None:
            return existing
        notebook = Notebook(
            id=new_id(), wax_id=wax_id, version=1, created_at=now, updated_at=now
        )
        self._write(
            wax_id,
            "INSERT INTO notebooks (id, wax_id, version, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (notebook.id, wax_id, 1, now, now),
        )
        return notebook

    def get_notebook(self, wax_id: WaxId) -> Notebook | None:
        row = self._one(
            wax_id,
            "SELECT id, wax_id, version, created_at, updated_at FROM notebooks "
            "WHERE wax_id = %s",
            (wax_id,),
        )
        if row is None:
            return None
        return Notebook(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _entry(self, row: Row) -> NotebookEntry:
        wax_id = WaxId(row["wax_id"])
        return NotebookEntry(
            id=row["id"],
            wax_id=wax_id,
            notebook_id=row["notebook_id"],
            schema_version=row["schema_version"],
            payload=self._dec_json(
                wax_id, "notebook.payload", row["payload_encrypted"]
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=NotebookEntryStatus(row["status"]),
            superseded_by_id=row["superseded_by_id"],
        )

    def add_notebook_entry(self, entry: NotebookEntry) -> NotebookEntry:
        self._write(
            entry.wax_id,
            f"INSERT INTO notebook_entries ({NOTEBOOK_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                entry.id,
                entry.wax_id,
                entry.notebook_id,
                entry.schema_version,
                self._enc_json(entry.wax_id, "notebook.payload", entry.payload),
                entry.status.value,
                entry.superseded_by_id,
                entry.created_at,
                entry.updated_at,
            ),
        )
        return entry

    def get_notebook_entry(self, wax_id: WaxId, entry_id: UUID) -> NotebookEntry | None:
        row = self._one(
            wax_id,
            f"SELECT {NOTEBOOK_COLUMNS} FROM notebook_entries WHERE wax_id = %s AND id = %s",
            (wax_id, entry_id),
        )
        return self._entry(row) if row else None

    def list_notebook_entries(
        self, wax_id: WaxId, notebook_id: UUID, *, include_superseded: bool = False
    ) -> tuple[NotebookEntry, ...]:
        rows = self._read(
            wax_id,
            f"SELECT {NOTEBOOK_COLUMNS} FROM notebook_entries "
            "WHERE wax_id = %s AND notebook_id = %s "
            "AND (%s OR status = 'active') ORDER BY created_at",
            (wax_id, notebook_id, include_superseded),
        )
        return tuple(self._entry(r) for r in rows)

    def supersede_notebook_entry(
        self, wax_id: WaxId, old_id: UUID, new_id: UUID, now: datetime
    ) -> NotebookEntry:
        if self.get_notebook_entry(wax_id, new_id) is None:
            raise ValueError("unknown notebook entry")
        changed = self._write(
            wax_id,
            "UPDATE notebook_entries SET status = 'superseded', "
            "superseded_by_id = %s, updated_at = %s WHERE wax_id = %s AND id = %s",
            (new_id, now, wax_id, old_id),
        )
        updated = self.get_notebook_entry(wax_id, old_id)
        if not changed or updated is None:
            raise ValueError("unknown notebook entry")
        return updated

    def invalidate_notebook_entry(
        self, wax_id: WaxId, entry_id: UUID, *, now: datetime, reason: str
    ) -> NotebookEntry:
        """Mark a notebook entry as invalidated (not superseded).

        The entry stays for audit; ordinary retrieval should exclude it.
        Caller is responsible for also dropping the entry's search tokens
        and embeddings. The invalidation is recorded as an Event so the
        audit trail captures the transition even if the entry row is later
        archived by retention policy. The free-form ``reason`` is not
        stored in event metadata (it can be student-derived).
        """
        changed = self._write(
            wax_id,
            "UPDATE notebook_entries SET status = 'invalidated', "
            "updated_at = %s WHERE wax_id = %s AND id = %s",
            (now, wax_id, entry_id),
        )
        updated = self.get_notebook_entry(wax_id, entry_id)
        if not changed or updated is None:
            raise ValueError("unknown notebook entry")
        self.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind="notebook_entry_invalidated",
                object_type=ObjectType.NOTEBOOK.value,
                object_id=entry_id,
                created_at=now,
                metadata={"status": NotebookEntryStatus.INVALIDATED.value},
            )
        )
        _ = reason
        return updated

    # --- summaries -----------------------------------------------------------------------------
    def _summary(self, row: Row) -> Summary:
        wax_id = WaxId(row["wax_id"])
        return Summary(
            id=row["id"],
            wax_id=wax_id,
            text=self._dec(wax_id, "summary.text", row["text_encrypted"]),
            covers_from=row["covers_from"],
            covers_until=row["covers_until"],
            level=row["level"],
            produced_by=row["produced_by"],
            created_at=row["created_at"],
            status=SummaryStatus(row["status"]),
        )

    def add_summary(self, summary: Summary) -> Summary:
        self._write(
            summary.wax_id,
            f"INSERT INTO summaries ({SUMMARY_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                summary.id,
                summary.wax_id,
                self._enc(summary.wax_id, "summary.text", summary.text),
                summary.covers_from,
                summary.covers_until,
                summary.level,
                summary.status.value,
                summary.produced_by,
                summary.created_at,
            ),
        )
        return summary

    def get_summary(self, wax_id: WaxId, summary_id: UUID) -> Summary | None:
        row = self._one(
            wax_id,
            f"SELECT {SUMMARY_COLUMNS} FROM summaries WHERE wax_id = %s AND id = %s",
            (wax_id, summary_id),
        )
        return self._summary(row) if row else None

    def list_summaries(
        self, wax_id: WaxId, *, include_superseded: bool = False, limit: int = 50
    ) -> tuple[Summary, ...]:
        rows = self._read(
            wax_id,
            f"SELECT {SUMMARY_COLUMNS} FROM summaries WHERE wax_id = %s "
            "AND (%s OR status = 'active') ORDER BY covers_until DESC LIMIT %s",
            (wax_id, include_superseded, limit),
        )
        return tuple(self._summary(r) for r in rows)

    def supersede_summary(
        self, wax_id: WaxId, summary_id: UUID, now: datetime
    ) -> Summary:
        del now
        changed = self._write(
            wax_id,
            "UPDATE summaries SET status = 'superseded' WHERE wax_id = %s AND id = %s",
            (wax_id, summary_id),
        )
        updated = self.get_summary(wax_id, summary_id)
        if not changed or updated is None:
            raise ValueError("unknown summary")
        return updated

    def invalidate_summary(
        self, wax_id: WaxId, summary_id: UUID, *, now: datetime, reason: str
    ) -> Summary:
        """Mark a summary as invalidated (not superseded). The free-form
        ``reason`` is not stored in event metadata."""
        changed = self._write(
            wax_id,
            "UPDATE summaries SET status = 'invalidated' WHERE wax_id = %s AND id = %s",
            (wax_id, summary_id),
        )
        updated = self.get_summary(wax_id, summary_id)
        if not changed or updated is None:
            raise ValueError("unknown summary")
        self.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind="summary_invalidated",
                object_type=ObjectType.SUMMARY.value,
                object_id=summary_id,
                created_at=now,
                metadata={"status": SummaryStatus.INVALIDATED.value},
            )
        )
        _ = reason
        return updated

    # --- provenance ------------------------------------------------------------------------------
    def add_provenance_links(self, links: tuple[ProvenanceLink, ...]) -> None:
        if not links:
            return
        wax_id = links[0].wax_id
        # Blocker F: polymorphic provenance links cannot be protected by
        # an ordinary FK, so the service must verify that every source
        # object belongs to the same student as the derived object.
        self._verify_provenance_ownership(wax_id, links)
        self._write_many(
            wax_id,
            f"INSERT INTO provenance_links ({LINK_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    link.id,
                    link.wax_id,
                    link.derived_type.value,
                    link.derived_id,
                    link.source_type.value,
                    link.source_id,
                    link.source_author_kind.value if link.source_author_kind else None,
                    self._enc(link.wax_id, "provenance.quote", link.quote)
                    if link.quote is not None
                    else None,
                    link.created_at,
                )
                for link in links
            ],
        )

    def _verify_provenance_ownership(
        self, wax_id: WaxId, links: tuple[ProvenanceLink, ...]
    ) -> None:
        """Reject provenance links whose source or derived object is owned
        by a different student. Polymorphic links cannot be FK-protected.
        """
        for link in links:
            if link.wax_id != wax_id:
                raise ValueError(
                    "provenance link wax_id does not match the batch owner"
                )
            self._check_object_owner(wax_id, link.source_type, link.source_id)
            self._check_object_owner(wax_id, link.derived_type, link.derived_id)

    def _check_object_owner(
        self, wax_id: WaxId, object_type: ObjectType, object_id: UUID
    ) -> None:
        """Verify the object is owned by `wax_id`. Raises ValueError if not.

        Uses the SECURITY DEFINER `waxprep_object_owner` function so the
        check sees the actual owner even when RLS would hide the row.
        """
        if not self._object_owner_table(object_type)[0]:
            return  # unstructured object types are not ownership-checked
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT waxprep_object_owner(%s, %s)",
                (object_type.value, object_id),
            )
            row = cur.fetchone()
        self._conn.commit()
        actual = row[0] if row else None
        if actual is None:
            # Could be missing (the object may not have been created yet) —
            # that's a separate error. We only reject cross-student refs.
            return
        if str(actual) != str(wax_id):
            raise ValueError(
                f"cross-student reference: {object_type.value} {object_id} "
                f"is owned by another student"
            )

    @staticmethod
    def _object_owner_table(object_type: ObjectType) -> tuple[str | None, str]:
        if object_type is ObjectType.HISTORY:
            return "history_entries", "id"
        if object_type is ObjectType.SUMMARY:
            return "summaries", "id"
        if object_type is ObjectType.NOTEBOOK:
            return "notebook_entries", "id"
        if object_type is ObjectType.KNOWLEDGE_NODE:
            return "knowledge_nodes", "id"
        if object_type is ObjectType.KNOWLEDGE_EDGE:
            return "knowledge_edges", "id"
        if object_type is ObjectType.ARTIFACT_VERSION:
            return "artifact_versions", "id"
        if object_type is ObjectType.ARTIFACT_CONTENT:
            return "artifact_contents", "id"
        return None, "id"

    def _link(self, row: Row) -> ProvenanceLink:
        wax_id = WaxId(row["wax_id"])
        quote = row["quote_encrypted"]
        kind = row["source_author_kind"]
        return ProvenanceLink(
            id=row["id"],
            wax_id=wax_id,
            derived_type=ObjectType(row["derived_type"]),
            derived_id=row["derived_id"],
            source_type=ObjectType(row["source_type"]),
            source_id=row["source_id"],
            created_at=row["created_at"],
            source_author_kind=AuthorKind(kind) if kind else None,
            quote=self._dec(wax_id, "provenance.quote", quote)
            if quote is not None
            else None,
        )

    def list_provenance(
        self, wax_id: WaxId, derived_type: ObjectType, derived_id: UUID
    ) -> tuple[ProvenanceLink, ...]:
        rows = self._read(
            wax_id,
            f"SELECT {LINK_COLUMNS} FROM provenance_links "
            "WHERE wax_id = %s AND derived_type = %s AND derived_id = %s "
            "ORDER BY created_at",
            (wax_id, derived_type.value, derived_id),
        )
        return tuple(self._link(r) for r in rows)

    def list_derived_from(
        self, wax_id: WaxId, source_type: ObjectType, source_id: UUID
    ) -> tuple[ProvenanceLink, ...]:
        rows = self._read(
            wax_id,
            f"SELECT {LINK_COLUMNS} FROM provenance_links "
            "WHERE wax_id = %s AND source_type = %s AND source_id = %s "
            "ORDER BY created_at",
            (wax_id, source_type.value, source_id),
        )
        return tuple(self._link(r) for r in rows)

    # --- knowledge graph ----------------------------------------------------------------------------
    def _node(self, row: Row) -> KnowledgeNode:
        wax_id = WaxId(row["wax_id"])
        description = row["description_encrypted"]
        return KnowledgeNode(
            id=row["id"],
            wax_id=wax_id,
            node_type=self._dec(
                wax_id, "knowledge.node_type", row["node_type_encrypted"]
            ),
            label=self._dec(wax_id, "knowledge.label", row["label_encrypted"]),
            description=(
                self._dec(wax_id, "knowledge.description", description)
                if description is not None
                else None
            ),
            properties=self._dec_json(
                wax_id, "knowledge.properties", row["properties_encrypted"]
            ),
            status=KnowledgeNodeStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_node(self, node: KnowledgeNode) -> KnowledgeNode:
        w = node.wax_id
        self._write(
            w,
            f"INSERT INTO knowledge_nodes ({NODE_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "node_type_encrypted = EXCLUDED.node_type_encrypted, "
            "label_encrypted = EXCLUDED.label_encrypted, "
            "description_encrypted = EXCLUDED.description_encrypted, "
            "properties_encrypted = EXCLUDED.properties_encrypted, "
            "status = EXCLUDED.status, updated_at = EXCLUDED.updated_at",
            (
                node.id,
                w,
                self._enc(w, "knowledge.node_type", node.node_type),
                self._enc(w, "knowledge.label", node.label),
                self._enc(w, "knowledge.description", node.description)
                if node.description is not None
                else None,
                self._enc_json(w, "knowledge.properties", node.properties),
                node.status.value,
                node.created_at,
                node.updated_at,
            ),
        )
        return node

    def get_node(self, wax_id: WaxId, node_id: UUID) -> KnowledgeNode | None:
        row = self._one(
            wax_id,
            f"SELECT {NODE_COLUMNS} FROM knowledge_nodes WHERE wax_id = %s AND id = %s",
            (wax_id, node_id),
        )
        return self._node(row) if row else None

    def _edge(self, row: Row) -> KnowledgeEdge:
        wax_id = WaxId(row["wax_id"])
        return KnowledgeEdge(
            id=row["id"],
            wax_id=wax_id,
            source_node_id=row["source_node_id"],
            target_node_id=row["target_node_id"],
            relation=self._dec(wax_id, "knowledge.relation", row["relation_encrypted"]),
            properties=self._dec_json(
                wax_id, "knowledge.edge_properties", row["properties_encrypted"]
            ),
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            created_at=row["created_at"],
        )

    def add_edge(self, edge: KnowledgeEdge) -> KnowledgeEdge:
        w = edge.wax_id
        self._write(
            w,
            f"INSERT INTO knowledge_edges ({EDGE_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                edge.id,
                w,
                edge.source_node_id,
                edge.target_node_id,
                self._enc(w, "knowledge.relation", edge.relation),
                self._enc_json(w, "knowledge.edge_properties", edge.properties),
                edge.valid_from,
                edge.valid_until,
                edge.created_at,
            ),
        )
        return edge

    def get_edge(self, wax_id: WaxId, edge_id: UUID) -> KnowledgeEdge | None:
        row = self._one(
            wax_id,
            f"SELECT {EDGE_COLUMNS} FROM knowledge_edges WHERE wax_id = %s AND id = %s",
            (wax_id, edge_id),
        )
        return self._edge(row) if row else None

    def list_edges(
        self, wax_id: WaxId, node_id: UUID, *, direction: str, limit: int
    ) -> tuple[KnowledgeEdge, ...]:
        clause = {
            "outgoing": "source_node_id = %s",
            "incoming": "target_node_id = %s",
        }.get(direction)
        if clause is None:
            rows = self._read(
                wax_id,
                f"SELECT {EDGE_COLUMNS} FROM knowledge_edges WHERE wax_id = %s "
                "AND (source_node_id = %s OR target_node_id = %s) "
                "ORDER BY created_at LIMIT %s",
                (wax_id, node_id, node_id, limit),
            )
        else:
            rows = self._read(
                wax_id,
                f"SELECT {EDGE_COLUMNS} FROM knowledge_edges WHERE wax_id = %s "
                f"AND {clause} ORDER BY created_at LIMIT %s",
                (wax_id, node_id, limit),
            )
        return tuple(self._edge(r) for r in rows)

    def invalidate_node(
        self, wax_id: WaxId, node_id: UUID, *, now: datetime, reason: str
    ) -> KnowledgeNode:
        """Mark a knowledge node as invalidated (not superseded). The free-form
        ``reason`` is not stored in event metadata."""
        changed = self._write(
            wax_id,
            "UPDATE knowledge_nodes SET status = 'invalidated', updated_at = %s "
            "WHERE wax_id = %s AND id = %s",
            (now, wax_id, node_id),
        )
        updated = self.get_node(wax_id, node_id)
        if not changed or updated is None:
            raise ValueError("unknown knowledge node")
        self.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind="knowledge_node_invalidated",
                object_type=ObjectType.KNOWLEDGE_NODE.value,
                object_id=node_id,
                created_at=now,
                metadata={"status": KnowledgeNodeStatus.INVALIDATED.value},
            )
        )
        _ = reason
        return updated

    # --- workspace and artifacts -----------------------------------------------------------------------
    def get_or_create_workspace(self, wax_id: WaxId, now: datetime) -> Workspace:
        existing = self.get_workspace(wax_id)
        if existing is not None:
            return existing
        workspace = Workspace(
            id=new_id(), wax_id=wax_id, created_at=now, updated_at=now
        )
        self._write(
            wax_id,
            "INSERT INTO workspaces (id, wax_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (workspace.id, wax_id, now, now),
        )
        return workspace

    def get_workspace(self, wax_id: WaxId) -> Workspace | None:
        row = self._one(
            wax_id,
            "SELECT id, wax_id, created_at, updated_at FROM workspaces WHERE wax_id = %s",
            (wax_id,),
        )
        if row is None:
            return None
        return Workspace(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _artifact(row: Row) -> Artifact:
        return Artifact(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            workspace_id=row["workspace_id"],
            status=ArtifactStatus(row["status"]),
            current_version_id=row["current_version_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_artifact(self, artifact: Artifact) -> Artifact:
        self._write(
            artifact.wax_id,
            f"INSERT INTO artifacts ({ARTIFACT_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                artifact.id,
                artifact.wax_id,
                artifact.workspace_id,
                artifact.status.value,
                artifact.current_version_id,
                artifact.created_at,
                artifact.updated_at,
            ),
        )
        return artifact

    def get_artifact(self, wax_id: WaxId, artifact_id: UUID) -> Artifact | None:
        row = self._one(
            wax_id,
            f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE wax_id = %s AND id = %s",
            (wax_id, artifact_id),
        )
        return self._artifact(row) if row else None

    def list_artifacts(self, wax_id: WaxId, workspace_id: UUID) -> tuple[Artifact, ...]:
        rows = self._read(
            wax_id,
            f"SELECT {ARTIFACT_COLUMNS} FROM artifacts "
            "WHERE wax_id = %s AND workspace_id = %s ORDER BY created_at",
            (wax_id, workspace_id),
        )
        return tuple(self._artifact(r) for r in rows)

    def _version(self, row: Row) -> ArtifactVersion:
        wax_id = WaxId(row["wax_id"])
        name = row["filename_encrypted"]
        return ArtifactVersion(
            id=row["id"],
            wax_id=wax_id,
            artifact_id=row["artifact_id"],
            version_number=row["version_number"],
            storage_ref=row["storage_ref"],
            original_filename=(
                self._dec(wax_id, "artifact.filename", name)
                if name is not None
                else None
            ),
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            checksum_sha256=row["checksum_sha256"],
            created_at=row["created_at"],
        )

    def create_artifact_version(self, version: ArtifactVersion) -> ArtifactVersion:
        name = (
            self._enc(version.wax_id, "artifact.filename", version.original_filename)
            if version.original_filename is not None
            else None
        )
        self._write(
            version.wax_id,
            f"INSERT INTO artifact_versions ({VERSION_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                version.id,
                version.wax_id,
                version.artifact_id,
                version.version_number,
                version.storage_ref,
                name,
                version.mime_type,
                version.size_bytes,
                version.checksum_sha256,
                version.created_at,
            ),
        )
        return version

    def get_artifact_version(
        self, wax_id: WaxId, artifact_id: UUID, version_id: UUID
    ) -> ArtifactVersion | None:
        row = self._one(
            wax_id,
            f"SELECT {VERSION_COLUMNS} FROM artifact_versions "
            "WHERE wax_id = %s AND artifact_id = %s AND id = %s",
            (wax_id, artifact_id, version_id),
        )
        return self._version(row) if row else None

    def get_artifact_version_by_id(
        self, wax_id: WaxId, version_id: UUID
    ) -> ArtifactVersion | None:
        row = self._one(
            wax_id,
            f"SELECT {VERSION_COLUMNS} FROM artifact_versions WHERE wax_id = %s AND id = %s",
            (wax_id, version_id),
        )
        return self._version(row) if row else None

    def list_artifact_versions(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactVersion, ...]:
        rows = self._read(
            wax_id,
            f"SELECT {VERSION_COLUMNS} FROM artifact_versions "
            "WHERE wax_id = %s AND artifact_id = %s ORDER BY version_number",
            (wax_id, artifact_id),
        )
        return tuple(self._version(r) for r in rows)

    def set_current_artifact_version(
        self,
        wax_id: WaxId,
        artifact_id: UUID,
        version_id: UUID,
        updated_at: datetime,
    ) -> Artifact:
        if self.get_artifact_version(wax_id, artifact_id, version_id) is None:
            raise ValueError("unknown artifact version")
        self._write(
            wax_id,
            "UPDATE artifacts SET current_version_id = %s, updated_at = %s "
            "WHERE wax_id = %s AND id = %s",
            (version_id, updated_at, wax_id, artifact_id),
        )
        artifact = self.get_artifact(wax_id, artifact_id)
        if artifact is None:
            raise ValueError("unknown artifact")
        return artifact

    def create_artifact_reference(
        self, reference: ArtifactReference
    ) -> ArtifactReference:
        self._write(
            reference.wax_id,
            "INSERT INTO artifact_references (id, wax_id, artifact_id, reference_type, "
            "reference_id, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                reference.id,
                reference.wax_id,
                reference.artifact_id,
                reference.reference_type,
                reference.reference_id,
                reference.created_at,
            ),
        )
        return reference

    def list_artifact_references(
        self, wax_id: WaxId, artifact_id: UUID
    ) -> tuple[ArtifactReference, ...]:
        rows = self._read(
            wax_id,
            "SELECT id, wax_id, artifact_id, reference_type, reference_id, created_at "
            "FROM artifact_references WHERE wax_id = %s AND artifact_id = %s "
            "ORDER BY created_at",
            (wax_id, artifact_id),
        )
        return tuple(
            ArtifactReference(
                id=r["id"],
                wax_id=WaxId(r["wax_id"]),
                artifact_id=r["artifact_id"],
                reference_type=r["reference_type"],
                reference_id=r["reference_id"],
                created_at=r["created_at"],
            )
            for r in rows
        )

    def _content(self, row: Row) -> ArtifactContent:
        wax_id = WaxId(row["wax_id"])
        return ArtifactContent(
            id=row["id"],
            wax_id=wax_id,
            artifact_version_id=row["artifact_version_id"],
            representation=row["representation"],
            text=self._dec(wax_id, "artifact_content.text", row["text_encrypted"]),
            produced_by=row["produced_by"],
            created_at=row["created_at"],
        )

    def add_artifact_content(self, content: ArtifactContent) -> ArtifactContent:
        self._write(
            content.wax_id,
            "INSERT INTO artifact_contents (id, wax_id, artifact_version_id, "
            "representation, text_encrypted, produced_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                content.id,
                content.wax_id,
                content.artifact_version_id,
                content.representation,
                self._enc(content.wax_id, "artifact_content.text", content.text),
                content.produced_by,
                content.created_at,
            ),
        )
        return content

    def get_artifact_content(
        self, wax_id: WaxId, content_id: UUID
    ) -> ArtifactContent | None:
        row = self._one(
            wax_id,
            "SELECT id, wax_id, artifact_version_id, representation, text_encrypted, "
            "produced_by, created_at FROM artifact_contents WHERE wax_id = %s AND id = %s",
            (wax_id, content_id),
        )
        return self._content(row) if row else None

    def list_artifact_content(
        self, wax_id: WaxId, artifact_version_id: UUID
    ) -> tuple[ArtifactContent, ...]:
        rows = self._read(
            wax_id,
            "SELECT id, wax_id, artifact_version_id, representation, text_encrypted, "
            "produced_by, created_at FROM artifact_contents "
            "WHERE wax_id = %s AND artifact_version_id = %s ORDER BY created_at",
            (wax_id, artifact_version_id),
        )
        return tuple(self._content(r) for r in rows)

    # --- background work -------------------------------------------------------------------------------------
    def enqueue_job(self, job: DerivationJob) -> DerivationJob:
        self._write(
            job.wax_id,
            f"INSERT INTO derivation_jobs ({JOB_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                job.id,
                job.wax_id,
                job.kind,
                job.object_type.value,
                job.object_id,
                job.status.value,
                job.attempts,
                job.last_error,
                job.run_after,
                job.created_at,
                job.updated_at,
                job.lease_until,
            ),
        )
        return job

    def list_wax_ids_with_due_jobs(
        self, now: datetime, limit: int
    ) -> tuple[WaxId, ...]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT wax_id FROM waxprep_students_with_due_jobs(%s, %s)",
                (now, limit),
            )
            rows: list[Row] = cur.fetchall()
        self._conn.commit()
        return tuple(WaxId(r["wax_id"]) for r in rows)

    def find_tutor_reply_to(
        self, wax_id: WaxId, student_entry_id: UUID
    ) -> HistoryEntry | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, wax_id, author_kind, content_kind, text_encrypted,
                       occurred_at, recorded_at, channel, channel_ref,
                       thread_ref, reply_to_id
                FROM history_entries
                WHERE wax_id = %s AND reply_to_id = %s AND author_kind = 'tutor'
                ORDER BY occurred_at, seq
                LIMIT 1
                """,
                (wax_id, student_entry_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._history(row)

    def claim_due_jobs(
        self, wax_id: WaxId, now: datetime, limit: int = 10, *, lease_seconds: int = 300
    ) -> tuple[DerivationJob, ...]:
        """Claim due jobs with row locks and lease_until (Blocker A)."""
        from datetime import timedelta

        lease_until = now + timedelta(seconds=lease_seconds)
        # RLS requires the current WAX ID to be set; otherwise the
        # derivation_jobs policy hides all rows.
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE derivation_jobs
                SET status = 'running',
                    lease_until = %s,
                    attempts = attempts + 1,
                    updated_at = %s
                WHERE id IN (
                    SELECT id FROM derivation_jobs
                    WHERE wax_id = %s
                      AND (
                        (status = 'pending' AND run_after <= %s)
                        OR (status = 'running' AND lease_until IS NOT NULL
                            AND lease_until <= %s)
                      )
                    ORDER BY run_after
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                RETURNING id, wax_id, kind, object_type, object_id, status,
                          attempts, last_error, run_after, created_at, updated_at,
                          lease_until
                """,
                (lease_until, now, wax_id, now, now, limit),
            )
            rows = cur.fetchall()
        if self._txn_depth == 0:
            self._conn.commit()
        jobs: list[DerivationJob] = []
        for r in rows:
            jobs.append(
                DerivationJob(
                    id=r["id"],
                    wax_id=r["wax_id"],
                    kind=r["kind"],
                    object_type=r["object_type"],
                    object_id=r["object_id"],
                    status=JobStatus(r["status"]),
                    attempts=r["attempts"],
                    last_error=r["last_error"],
                    run_after=r["run_after"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    lease_until=r.get("lease_until")
                    if hasattr(r, "get")
                    else r["lease_until"],
                )
            )
        return tuple(jobs)

    def release_job_capability_skip(
        self, wax_id: WaxId, job_id: UUID, now: datetime, retry_at: datetime
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE derivation_jobs
                SET status = 'pending',
                    attempts = GREATEST(attempts - 1, 0),
                    last_error = 'capability_unavailable',
                    run_after = %s,
                    updated_at = %s,
                    lease_until = NULL
                WHERE wax_id = %s AND id = %s
                """,
                (retry_at, now, wax_id, job_id),
            )
        self._conn.commit()

    def mark_job_done(self, wax_id: WaxId, job_id: UUID, now: datetime) -> None:
        # Success does not increment attempts; the attempt was counted at
        # claim time.
        self._write(
            wax_id,
            "UPDATE derivation_jobs SET status = 'done', lease_until = NULL, "
            "updated_at = %s WHERE wax_id = %s AND id = %s",
            (now, wax_id, job_id),
        )

    def mark_job_failed(
        self,
        wax_id: WaxId,
        job_id: UUID,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        # Blocker G (spec 8.2-#10): the attempt was already counted at
        # claim time. Failure records the result of the same attempt;
        # it must NOT increment attempts again. One real execution
        # attempt counts exactly once.
        self._write(
            wax_id,
            "UPDATE derivation_jobs SET last_error = %s, "
            "status = %s, run_after = COALESCE(%s, run_after), "
            "lease_until = NULL, updated_at = %s "
            "WHERE wax_id = %s AND id = %s",
            (
                error,
                "pending" if retry_at is not None else "failed",
                retry_at,
                now,
                wax_id,
                job_id,
            ),
        )

    # --- audit ------------------------------------------------------------------------------------------------------
    def record_investigation(self, record: InvestigationRecord) -> InvestigationRecord:
        w = record.wax_id
        self._write(
            w,
            "INSERT INTO context_investigations (id, wax_id, trigger_entry_id, "
            "input_encrypted, result_encrypted, model, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                record.id,
                w,
                record.trigger_entry_id,
                self._enc(w, "investigation.input", record.input_text),
                self._enc_json(w, "investigation.result", record.result),
                record.model,
                record.created_at,
            ),
        )
        return record

    def append_event(self, event: Event) -> Event:
        from waxprep.security.event_metadata import validate_event_metadata

        validated = validate_event_metadata(event.metadata)
        self._write(
            event.wax_id,
            "INSERT INTO events (id, wax_id, kind, object_type, object_id, created_at, "
            "metadata) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                event.id,
                event.wax_id,
                event.kind,
                event.object_type,
                event.object_id,
                event.created_at,
                json.dumps(validated),
            ),
        )
        return event

    # --- outbound delivery (outbox) ------------------------------------------------
    def create_outbound_delivery(self, delivery: OutboundDelivery) -> OutboundDelivery:
        set_current_wax_id(self._conn, delivery.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outbound_deliveries "
                "(id, wax_id, tutor_entry_id, channel, channel_ref, "
                "provider_message_id, status, attempts, last_error, "
                "delivered_at, run_after, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    delivery.id,
                    delivery.wax_id,
                    delivery.tutor_entry_id,
                    delivery.channel,
                    delivery.channel_ref,
                    delivery.provider_message_id,
                    delivery.status.value,
                    delivery.attempts,
                    delivery.last_error,
                    delivery.delivered_at,
                    delivery.run_after,
                    delivery.created_at,
                    delivery.updated_at,
                ),
            )
            if cur.rowcount == 0:
                # Duplicate (wax_id, tutor_entry_id, channel) — return
                # the existing one.
                cur.execute(
                    "SELECT * FROM outbound_deliveries "
                    "WHERE wax_id = %s AND tutor_entry_id = %s AND channel IS NOT DISTINCT FROM %s",
                    (delivery.wax_id, delivery.tutor_entry_id, delivery.channel),
                )
                row = cur.fetchone()
                if row is not None:
                    self._conn.commit()
                    return self._outbound(row)
        self._conn.commit()
        return delivery

    def get_outbound_delivery_for_tutor(
        self, wax_id: WaxId, tutor_entry_id: UUID, channel: str | None
    ) -> OutboundDelivery | None:
        row = self._one(
            wax_id,
            "SELECT * FROM outbound_deliveries "
            "WHERE wax_id = %s AND tutor_entry_id = %s "
            "AND channel IS NOT DISTINCT FROM %s",
            (wax_id, tutor_entry_id, channel),
        )
        return self._outbound(row) if row else None

    def list_due_outbound_deliveries(
        self, wax_id: WaxId | None, now: datetime, limit: int
    ) -> tuple[OutboundDelivery, ...]:
        # Outbox polls globally (wax_id=None). RLS is tenant-scoped, so a
        # global scan briefly uses the connection owner (bypasses RLS), then
        # restores the app role. Per-tenant calls stay under normal RLS.
        if wax_id is None:
            from waxprep.storage.session import APP_ROLE

            try:
                if self._conn.info.transaction_status != 0:  # not IDLE
                    self._conn.rollback()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
            with self._conn.cursor(row_factory=dict_row) as cur:
                cur.execute("RESET ROLE")
                cur.execute(
                    "SELECT * FROM outbound_deliveries "
                    "WHERE status IN ('pending', 'in_progress') "
                    "AND run_after <= %s "
                    "ORDER BY run_after LIMIT %s",
                    (now, limit),
                )
                rows = cur.fetchall()
                cur.execute(f"SET ROLE {APP_ROLE}")
            if self._txn_depth == 0:
                self._conn.commit()
            return tuple(self._outbound(r) for r in rows)
        rows = self._read(
            wax_id,
            "SELECT * FROM outbound_deliveries WHERE wax_id = %s "
            "AND status = 'pending' AND run_after <= %s "
            "ORDER BY run_after LIMIT %s",
            (wax_id, now, limit),
        )
        return tuple(self._outbound(r) for r in rows)

    def claim_outbound_delivery(
        self, wax_id: WaxId, delivery_id: UUID, now: datetime
    ) -> OutboundDelivery | None:
        """CAS claim: pending → in_progress. Only one winner across replicas."""
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE outbound_deliveries SET status = 'in_progress', "
                "updated_at = %s "
                "WHERE wax_id = %s AND id = %s AND status = 'pending' "
                "RETURNING *",
                (now, wax_id, delivery_id),
            )
            row = cur.fetchone()
        if self._txn_depth == 0:
            self._conn.commit()
        if row is None:
            return None
        return self._outbound(row)

    @staticmethod
    def _turn_lock_keys(wax_id: WaxId, student_entry_id: UUID) -> tuple[int, int]:
        import hashlib
        import struct

        digest = hashlib.sha256(f"turn:{wax_id}:{student_entry_id}".encode()).digest()
        k1, k2 = struct.unpack(">ii", digest[:8])
        return k1, k2

    def try_acquire_turn_lock(self, wax_id: WaxId, student_entry_id: UUID) -> bool:
        """Session-level advisory lock; works across Railway replicas."""
        k1, k2 = self._turn_lock_keys(wax_id, student_entry_id)
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (k1, k2))
            row = cur.fetchone()
        # Advisory locks survive beyond the transaction; do not leave an
        # open transaction. Commit/rollback keeps connection healthy.
        if self._txn_depth == 0:
            try:
                self._conn.commit()
            except Exception:
                pass
        return bool(row and row[0])

    def release_turn_lock(self, wax_id: WaxId, student_entry_id: UUID) -> None:
        k1, k2 = self._turn_lock_keys(wax_id, student_entry_id)
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", (k1, k2))
        if self._txn_depth == 0:
            try:
                self._conn.commit()
            except Exception:
                pass

    def mark_outbound_delivered(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        provider_message_id: str | None = None,
    ) -> None:
        # Delivery attempts are counted in the channel adapter; this
        # method records the final delivered state without incrementing
        # attempts (the channel adapter updates attempts separately).
        if provider_message_id is not None:
            self._write(
                wax_id,
                "UPDATE outbound_deliveries SET status = 'delivered', "
                "provider_message_id = %s, delivered_at = %s, updated_at = %s "
                "WHERE wax_id = %s AND id = %s",
                (provider_message_id, now, now, wax_id, delivery_id),
            )
        else:
            self._write(
                wax_id,
                "UPDATE outbound_deliveries SET status = 'delivered', "
                "delivered_at = %s, updated_at = %s WHERE wax_id = %s AND id = %s",
                (now, now, wax_id, delivery_id),
            )

    def mark_outbound_failed(
        self,
        wax_id: WaxId,
        delivery_id: UUID,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        self._write(
            wax_id,
            "UPDATE outbound_deliveries SET last_error = %s, "
            "status = %s, run_after = COALESCE(%s, run_after), updated_at = %s "
            "WHERE wax_id = %s AND id = %s",
            (
                error,
                "pending" if retry_at is not None else "failed",
                retry_at,
                now,
                wax_id,
                delivery_id,
            ),
        )

    # --- interactive offers ---------------------------------------------------------
    def create_interactive_offer(self, offer: InteractiveOffer) -> InteractiveOffer:
        set_current_wax_id(self._conn, offer.wax_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO interactive_offers "
                "(id, wax_id, tutor_entry_id, provider_outbound_message_id, "
                "buttons, status, created_at, updated_at, expires_at, "
                "consumed_button_id, consumed_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)",
                (
                    offer.id,
                    offer.wax_id,
                    offer.tutor_entry_id,
                    offer.provider_outbound_message_id,
                    json.dumps(list(offer.buttons)),
                    offer.status.value,
                    offer.created_at,
                    offer.updated_at,
                    offer.expires_at,
                    offer.consumed_button_id,
                    offer.consumed_at,
                ),
            )
        self._conn.commit()
        return offer

    def find_interactive_offer_by_button_id(
        self, wax_id: WaxId, button_id: str
    ) -> InteractiveOffer | None:
        # buttons is JSONB array of {id, title}; search with jsonb containment.
        rows = self._read(
            wax_id,
            "SELECT * FROM interactive_offers "
            "WHERE wax_id = %s AND buttons @> %s::jsonb "
            "ORDER BY created_at DESC LIMIT 1",
            (wax_id, json.dumps([{"id": button_id}])),
        )
        if not rows:
            return None
        return self._interactive_offer(rows[0])

    def consume_interactive_offer(
        self,
        wax_id: WaxId,
        offer_id: UUID,
        button_id: str,
        now: datetime,
    ) -> InteractiveOffer | None:
        set_current_wax_id(self._conn, wax_id)
        with self._conn.cursor(row_factory=dict_row) as cur:
            # Expire if past expires_at while still open.
            cur.execute(
                "UPDATE interactive_offers SET status = 'expired', updated_at = %s "
                "WHERE wax_id = %s AND id = %s AND status = 'open' "
                "AND expires_at IS NOT NULL AND expires_at <= %s",
                (now, wax_id, offer_id, now),
            )
            cur.execute(
                "UPDATE interactive_offers SET status = 'consumed', "
                "consumed_button_id = %s, consumed_at = %s, updated_at = %s "
                "WHERE wax_id = %s AND id = %s AND status = 'open' "
                "RETURNING *",
                (button_id, now, now, wax_id, offer_id),
            )
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            return None
        return self._interactive_offer(row)

    def _interactive_offer(self, row: Row) -> InteractiveOffer:
        buttons_raw = row["buttons"]
        if isinstance(buttons_raw, str):
            buttons_raw = json.loads(buttons_raw)
        buttons = tuple(
            {"id": str(b.get("id", "")), "title": str(b.get("title", ""))}
            for b in (buttons_raw or [])
        )
        return InteractiveOffer(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            tutor_entry_id=row.get("tutor_entry_id"),
            provider_outbound_message_id=row.get("provider_outbound_message_id"),
            buttons=buttons,
            status=InteractiveOfferStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row.get("expires_at"),
            consumed_button_id=row.get("consumed_button_id"),
            consumed_at=row.get("consumed_at"),
        )

    def _outbound(self, row: Row) -> OutboundDelivery:
        return OutboundDelivery(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            tutor_entry_id=row["tutor_entry_id"],
            channel=row["channel"],
            channel_ref=row["channel_ref"],
            provider_message_id=row.get("provider_message_id"),
            status=OutboundStatus(row["status"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            delivered_at=row["delivered_at"],
            run_after=row["run_after"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_events(self, wax_id: WaxId) -> tuple[Event, ...]:
        rows = self._read(
            wax_id,
            "SELECT id, wax_id, kind, object_type, object_id, created_at, metadata "
            "FROM events WHERE wax_id = %s ORDER BY created_at",
            (wax_id,),
        )
        out: list[Event] = []
        for r in rows:
            meta = r["metadata"]
            if isinstance(meta, str):
                meta = json.loads(meta)
            out.append(
                Event(
                    id=r["id"],
                    wax_id=WaxId(r["wax_id"]) if r["wax_id"] else None,
                    kind=r["kind"],
                    object_type=r["object_type"],
                    object_id=r["object_id"],
                    created_at=r["created_at"],
                    metadata=dict(meta),
                )
            )
        return tuple(out)

    # --- deletion requests (Blueprint Part IV §16-17) -------------------------------
    def _deletion_request(self, row: Row) -> DeletionRequest:
        propagation_log = row.get("propagation_log") or {}
        if isinstance(propagation_log, str):
            propagation_log = json.loads(propagation_log)
        return DeletionRequest(
            id=row["id"],
            wax_id=WaxId(row["wax_id"]),
            object_type=ObjectType(row["object_type"]),
            object_id=row["object_id"],
            scope=row["scope"],
            reason=row.get("reason"),
            status=DeletionRequestStatus(row["status"]),
            requested_at=row["requested_at"],
            completed_at=row.get("completed_at"),
            propagation_log=dict(propagation_log or {}),
        )

    def create_deletion_request(self, request: DeletionRequest) -> DeletionRequest:
        self._write(
            request.wax_id,
            "INSERT INTO deletion_requests (id, wax_id, object_type, object_id, "
            "scope, reason, status, requested_at, completed_at, propagation_log) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                request.id,
                request.wax_id,
                request.object_type.value,
                request.object_id,
                request.scope,
                request.reason,
                request.status.value,
                request.requested_at,
                request.completed_at,
                json.dumps(request.propagation_log),
            ),
        )
        # Record the request creation as an Event so the audit trail has
        # it even after the request row is removed by retention. The
        # sensitive ``scope`` and ``reason`` text lives on the request
        # row itself, encrypted at rest where applicable. The event
        # carries only operational metadata.
        self.append_event(
            Event(
                id=new_id(),
                wax_id=request.wax_id,
                kind="deletion_request_created",
                object_type=request.object_type.value,
                object_id=request.object_id,
                created_at=request.requested_at,
                metadata={
                    "status": request.status.value,
                    "request_id": str(request.id),
                },
            )
        )
        return request

    def get_deletion_request(
        self, wax_id: WaxId, request_id: UUID
    ) -> DeletionRequest | None:
        rows = self._read(
            wax_id,
            "SELECT id, wax_id, object_type, object_id, scope, reason, status, "
            "requested_at, completed_at, propagation_log FROM deletion_requests "
            "WHERE wax_id = %s AND id = %s",
            (wax_id, request_id),
        )
        return self._deletion_request(rows[0]) if rows else None

    def list_deletion_requests(
        self, wax_id: WaxId, *, status: str | None = None, limit: int = 50
    ) -> tuple[DeletionRequest, ...]:
        if status is None:
            rows = self._read(
                wax_id,
                "SELECT id, wax_id, object_type, object_id, scope, reason, status, "
                "requested_at, completed_at, propagation_log FROM deletion_requests "
                "WHERE wax_id = %s ORDER BY requested_at DESC LIMIT %s",
                (wax_id, limit),
            )
        else:
            rows = self._read(
                wax_id,
                "SELECT id, wax_id, object_type, object_id, scope, reason, status, "
                "requested_at, completed_at, propagation_log FROM deletion_requests "
                "WHERE wax_id = %s AND status = %s ORDER BY requested_at DESC LIMIT %s",
                (wax_id, status, limit),
            )
        return tuple(self._deletion_request(r) for r in rows)

    def list_wax_ids_with_pending_deletion_requests(
        self, limit: int
    ) -> tuple[WaxId, ...]:
        """Distinct wax_ids that have at least one PENDING deletion request.

        Uses the ``waxprep_wax_ids_with_pending_deletions`` SECURITY
        DEFINER function to bypass RLS — the worker (app role, no
        current_wax_id set) cannot read deletion_requests directly
        because RLS enforces ``wax_id = current_setting(...)``. The
        function returns only the wax_id column; no sensitive content
        crosses the isolation boundary.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT wax_id FROM waxprep_wax_ids_with_pending_deletions(%s)",
                (limit,),
            )
            rows: list[Row] = cur.fetchall()
        if self._txn_depth == 0:
            self._conn.commit()
        return tuple(WaxId(r["wax_id"]) for r in rows)

    def update_deletion_request(self, request: DeletionRequest) -> DeletionRequest:
        changed = self._write(
            request.wax_id,
            "UPDATE deletion_requests SET scope = %s, reason = %s, status = %s, "
            "completed_at = %s, propagation_log = %s::jsonb "
            "WHERE wax_id = %s AND id = %s",
            (
                request.scope,
                request.reason,
                request.status.value,
                request.completed_at,
                json.dumps(request.propagation_log),
                request.wax_id,
                request.id,
            ),
        )
        if not changed:
            raise ValueError("unknown deletion request")
        self.append_event(
            Event(
                id=new_id(),
                wax_id=request.wax_id,
                kind="deletion_request_updated",
                object_type=request.object_type.value,
                object_id=request.object_id,
                created_at=request.completed_at or request.requested_at,
                metadata={
                    "status": request.status.value,
                    "request_id": str(request.id),
                },
            )
        )
        return request

    def claim_deletion_request(
        self,
        wax_id: WaxId,
        request_id: UUID,
        *,
        now: datetime,
        propagation_log: dict[str, Any],
    ) -> DeletionRequest | None:
        """Atomic conditional UPDATE: PENDING → IN_PROGRESS.

        Uses ``UPDATE ... WHERE status = 'pending' RETURNING *`` so
        that only one of N racing workers can win the claim — the
        others get 0 rows and return None. The claim participates in
        the caller's transaction (set via ``transaction()``); if the
        caller rolls back, the claim is undone, which is exactly what
        we want when propagation fails (no stuck in_progress state).
        """
        _ = now  # requested_at is preserved; we do not overwrite it.
        rows = self._read(
            wax_id,
            "UPDATE deletion_requests SET status = 'in_progress', "
            "propagation_log = %s::jsonb "
            "WHERE wax_id = %s AND id = %s AND status = 'pending' "
            "RETURNING id, wax_id, object_type, object_id, scope, reason, "
            "status, requested_at, completed_at, propagation_log",
            (
                json.dumps(propagation_log),
                wax_id,
                request_id,
            ),
        )
        if not rows:
            return None
        return self._deletion_request(rows[0])


__all__ = ["PostgresStorage"]
