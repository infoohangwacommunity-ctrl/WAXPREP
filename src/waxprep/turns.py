"""One student turn, end to end.

Save what the student said -> investigate -> teach -> save what the tutor said.
The student's message is saved before anything else can fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from waxprep.clock import Clock
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.models import ContextPackage, InvestigationRequest
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, HistoryEntry, InvestigationRecord, new_id
from waxprep.history.service import HistoryService
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


class Teacher(Protocol):
    async def reply(
        self, wax_id: WaxId, message: HistoryEntry, context: ContextPackage
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TurnResult:
    student_entry: HistoryEntry
    tutor_entry: HistoryEntry | None
    context: ContextPackage
    duplicate: bool = False


class TurnService:
    def __init__(
        self,
        storage: Storage,
        history: HistoryService,
        investigator: ContextInvestigator,
        teacher: Teacher,
        clock: Clock,
        *,
        context_model_name: str = "context",
    ) -> None:
        self._storage = storage
        self._history = history
        self._investigator = investigator
        self._teacher = teacher
        self._clock = clock
        self._context_model_name = context_model_name

    async def handle_student_message(
        self,
        wax_id: WaxId,
        text: str,
        *,
        channel: str | None = None,
        channel_ref: str | None = None,
        thread_ref: str | None = None,
        occurred_at: datetime | None = None,
    ) -> TurnResult:
        student_entry, created = self._history.record_with_status(
            wax_id,
            AuthorKind.STUDENT,
            text,
            channel=channel,
            channel_ref=channel_ref,
            thread_ref=thread_ref,
            occurred_at=occurred_at,
        )
        if not created:
            # Duplicate channel delivery. If a tutor reply was already
            # saved, return it without re-teaching. If the previous
            # attempt crashed before the reply was saved, resume
            # investigation and teaching for this trigger entry. Either
            # way, this delivery was a duplicate — `duplicate=True`.
            existing_reply = self._storage.find_tutor_reply_to(wax_id, student_entry.id)
            if existing_reply is not None:
                return TurnResult(
                    student_entry,
                    existing_reply,
                    ContextPackage(
                        wax_id=wax_id,
                        query=text,
                        evidence=(),
                        summary="Duplicate delivery; tutor reply already stored.",
                        stopped_reason="duplicate_delivery",
                        as_of=self._clock.now(),
                    ),
                    duplicate=True,
                )
        package = await self._investigate(wax_id, student_entry)
        reply_text = await self._teacher.reply(wax_id, student_entry, package)
        tutor_entry = self._history.record(
            wax_id,
            AuthorKind.TUTOR,
            reply_text,
            channel=channel,
            thread_ref=thread_ref,
            reply_to_id=student_entry.id,
        )
        return TurnResult(
            student_entry, tutor_entry, package, duplicate=not created
        )

    async def _investigate(self, wax_id: WaxId, entry: HistoryEntry) -> ContextPackage:
        now = self._clock.now()
        try:
            package = await self._investigator.investigate(
                InvestigationRequest(
                    wax_id=wax_id,
                    query=entry.text or "",
                    now=now,
                    trigger_entry_id=entry.id,
                )
            )
        except Exception:
            logger.exception("context investigation failed; teaching without it")
            return ContextPackage(
                wax_id=wax_id,
                query=entry.text or "",
                evidence=(),
                summary="Context was unavailable for this message.",
                uncertainty=("context_unavailable",),
                stopped_reason="investigation_failed",
                as_of=now,
            )
        try:
            self._storage.record_investigation(
                InvestigationRecord(
                    id=new_id(),
                    wax_id=wax_id,
                    trigger_entry_id=entry.id,
                    input_text=package.query,
                    result={
                        "summary": package.summary,
                        "evidence_ids": [str(e.id) for e in package.evidence],
                        "uncertainty": list(package.uncertainty),
                        "investigated": list(package.investigated),
                    },
                    model=self._context_model_name,
                    created_at=now,
                )
            )
        except Exception:
            logger.exception("could not record investigation audit")
        return package
