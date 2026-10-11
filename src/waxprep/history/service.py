"""Saving everything that happens, first and unconditionally."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, HistoryEntry, ObjectType, new_id
from waxprep.index.indexer import Indexer
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


class HistoryService:
    """Records both sides of every exchange.

    Order of operations is the whole design:
    1. Save the entry. If this fails, the caller must know.
    2. Everything else (words index, embeddings, summaries) is best-effort
       and can never remove or block the saved entry.

    The service never asks whether a message is "important". A message that
    looks pointless today may be exactly what the student refers to in six
    months, so nothing is judged at save time.
    """

    def __init__(self, storage: Storage, indexer: Indexer, clock: Clock) -> None:
        self._storage = storage
        self._indexer = indexer
        self._clock = clock

    def record(
        self, wax_id: WaxId, author_kind: AuthorKind, text: str | None, **kwargs: Any
    ) -> HistoryEntry:
        """Save an entry and return it (see ``record_with_status``)."""
        return self.record_with_status(wax_id, author_kind, text, **kwargs)[0]

    def record_with_status(
        self,
        wax_id: WaxId,
        author_kind: AuthorKind,
        text: str | None,
        *,
        content_kind: str = "text/plain",
        occurred_at: datetime | None = None,
        channel: str | None = None,
        channel_ref: str | None = None,
        thread_ref: str | None = None,
        reply_to_id: UUID | None = None,
    ) -> tuple[HistoryEntry, bool]:
        """Save an entry. Returns (entry, created); created is False for a
        duplicate delivery of something already saved."""
        now = self._clock.now()
        entry = HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=author_kind,
            content_kind=content_kind,
            text=text,
            occurred_at=occurred_at or now,
            recorded_at=now,
            channel=channel,
            channel_ref=channel_ref,
            thread_ref=thread_ref,
            reply_to_id=reply_to_id,
        )
        saved = self._storage.append_history_entry(entry)
        if saved.id != entry.id:
            return saved, False  # duplicate delivery from the channel
        if saved.text:
            try:
                self._indexer.index(
                    wax_id,
                    ObjectType.HISTORY,
                    saved.id,
                    saved.text,
                    saved.occurred_at,
                )
            except Exception:
                logger.exception("indexing failed after save; entry is safe")
        return saved, True
