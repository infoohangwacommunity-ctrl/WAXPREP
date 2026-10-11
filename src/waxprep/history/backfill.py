"""One-off copy of old `messages` rows into history_entries (before migration 006).

Only needed if a database already holds real messages. Ids are preserved so
attachments keep pointing at the right entries.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, HistoryEntry
from waxprep.storage.postgres import PostgresStorage


def backfill_messages(conn: psycopg.Connection[Any], storage: PostgresStorage) -> int:
    """Copy every old message into history_entries. Safe to run repeatedly."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, wax_id, role, content_type, text_body, created_at "
            "FROM messages ORDER BY created_at"
        )
        rows: list[dict[str, Any]] = cur.fetchall()
    copied = 0
    for row in rows:
        wax_id = WaxId(row["wax_id"])
        if storage.get_history_entry(wax_id, row["id"]) is not None:
            continue
        role = str(row["role"]).lower()
        kind = (
            AuthorKind.TUTOR
            if role in ("tutor", "assistant")
            else AuthorKind.SYSTEM
            if role == "system"
            else AuthorKind.STUDENT
        )
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
    return copied
