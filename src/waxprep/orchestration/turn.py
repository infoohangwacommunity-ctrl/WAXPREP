"""Teacher-first turn orchestration.

The architecture is:

    Student message arrives
        |
        v
    Save exact student message (atomic with index jobs)
        |
        v
    Teacher drives the turn
        |   may consult Context Intelligence (bounded)
        |   may request another investigation (bounded)
        v
    Teacher eventually produces a reply
        |
        v
    Save exactly one tutor reply (linked to the student entry)
        |
        v
    Create outbound delivery record (outbox)
        |
        v
    (Channel adapter delivers; on crash, retry sends the same reply)

The exact student message reaches the Teacher untouched. CI may receive
the original text as part of an investigation request, but the
Teacher always holds the original ``HistoryEntry`` and CI's synthesis
never replaces it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from waxprep.clock import Clock
from waxprep.context.investigator import ContextInvestigator
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    OutboundDelivery,
    OutboundStatus,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.orchestration.consultation import CIBackedConsultation
from waxprep.orchestration.contracts import (
    Teacher,
    TeacherConsultation,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome of a student turn."""

    student_entry: HistoryEntry
    tutor_entry: HistoryEntry | None
    context_summary: str
    duplicate: bool
    consultations_made: int
    outbound_delivery_id: Any | None = None


class TurnService:
    """Teacher-first turn orchestrator.

    The Teacher decides whether to consult CI, what to ask, and whether
    the result is sufficient. The orchestrator enforces a configurable
    hard upper bound on the number of consultations per turn (default 3)
    so a buggy Teacher cannot enter an infinite CI loop.
    """

    def __init__(
        self,
        storage: Storage,
        history: HistoryService,
        investigator: ContextInvestigator,
        teacher: Teacher,
        clock: Clock,
        *,
        max_consultations: int = 3,
        context_model_name: str = "context",
    ) -> None:
        if max_consultations < 1:
            raise ValueError("max_consultations must be at least 1")
        self._storage = storage
        self._history = history
        self._investigator = investigator
        self._teacher = teacher
        self._clock = clock
        self._max_consultations = max_consultations
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
        """Process one student turn end-to-end.

        Order of operations:
        1. Save the exact student message (atomic with index jobs).
           If the DB fails here, the channel adapter must allow a
           provider retry — no acknowledgment.
        2. Duplicate channel delivery: if a tutor reply already exists,
           return it without re-teaching. If the previous attempt
           crashed before the reply was saved, resume teaching.
        3. Teacher drives the turn with bounded CI consultations.
        4. Save exactly one tutor reply (linked to student entry).
        5. Create outbound delivery record (outbox).
        """
        # 1. Save exact student message.
        student_entry, created = self._history.record_with_status(
            wax_id,
            AuthorKind.STUDENT,
            text,
            channel=channel,
            channel_ref=channel_ref,
            thread_ref=thread_ref,
            occurred_at=occurred_at,
        )

        # 2. Duplicate delivery handling.
        if not created:
            existing_reply = self._storage.find_tutor_reply_to(wax_id, student_entry.id)
            if existing_reply is not None:
                # Also look for an existing outbound delivery to return.
                outbound = self._storage.get_outbound_delivery_for_tutor(
                    wax_id, existing_reply.id, channel
                )
                return TurnResult(
                    student_entry=student_entry,
                    tutor_entry=existing_reply,
                    context_summary="Duplicate delivery; tutor reply already stored.",
                    duplicate=True,
                    consultations_made=0,
                    outbound_delivery_id=(
                        outbound.id if outbound is not None else None
                    ),
                )
            # Previous attempt crashed before tutor reply was saved;
            # resume teaching below.

        # 3. Teacher drives the turn with bounded consultations.
        consultation: TeacherConsultation = CIBackedConsultation(
            self._investigator,
            storage=self._storage,
            context_model_name=self._context_model_name,
        )
        reply_text, consultations_made, context_summary = await self._run_teacher(
            wax_id, student_entry, consultation
        )

        # If the Teacher declined to reply, finish without a tutor entry.
        if reply_text is None:
            return TurnResult(
                student_entry=student_entry,
                tutor_entry=None,
                context_summary=context_summary,
                duplicate=not created,
                consultations_made=consultations_made,
            )

        # 4. Save exactly one tutor reply (linked to student entry).
        tutor_entry = self._history.record(
            wax_id,
            AuthorKind.TUTOR,
            reply_text,
            channel=channel,
            thread_ref=thread_ref,
            reply_to_id=student_entry.id,
        )

        # 5. Create outbound delivery record (outbox).
        outbound_id = None
        if channel is not None:
            now = self._clock.now()
            delivery = self._storage.create_outbound_delivery(
                OutboundDelivery(
                    id=new_id(),
                    wax_id=wax_id,
                    tutor_entry_id=tutor_entry.id,
                    channel=channel,
                    channel_ref=channel_ref,
                    status=OutboundStatus.PENDING,
                    attempts=0,
                    last_error=None,
                    delivered_at=None,
                    run_after=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            outbound_id = delivery.id

        return TurnResult(
            student_entry=student_entry,
            tutor_entry=tutor_entry,
            context_summary=context_summary,
            duplicate=not created,
            consultations_made=consultations_made,
            outbound_delivery_id=outbound_id,
        )

    async def _run_teacher(
        self,
        wax_id: WaxId,
        student_entry: HistoryEntry,
        consultation: TeacherConsultation,
    ) -> tuple[str | None, int, str]:
        """Run the Teacher with bounded consultations.

        Returns ``(reply_text, consultations_made, context_summary)``.
        ``reply_text`` is ``None`` if the Teacher declined to respond.
        """
        prior_results: list = []
        consultations_made = 0
        last_summary = ""
        for round_number in range(self._max_consultations):
            now = self._clock.now()
            inp = TeacherInput(
                wax_id=wax_id,
                original_message=student_entry,
                now=now,
                consultation=consultation,
                prior_results=tuple(prior_results),
                round_number=round_number,
                max_rounds=self._max_consultations,
            )
            outcome: TeacherOutcome = await self._teacher.teach(inp)
            prior_results = list(outcome.context_used)
            if outcome.context_used:
                # The Teacher actually consulted CI on this round.
                consultations_made += len(outcome.context_used) - len(inp.prior_results)
            if outcome.finished:
                if outcome.reply_text is None:
                    # Teacher declined to respond.
                    return None, consultations_made, last_summary
                last_summary = self._summarize_context(prior_results)
                return outcome.reply_text, consultations_made, last_summary
            # Teacher wants another round. If they asked a question,
            # the consultation adapter will run via the next iteration's
            # Teacher.teach() call (the mock Teacher does this; a real
            # Teacher implementation should call inp.consultation.consult()
            # directly to gather context before finishing).
            last_summary = self._summarize_context(prior_results)
        # Hit the consultation bound; ask the Teacher one last time
        # with a finished=True expectation.
        now = self._clock.now()
        inp = TeacherInput(
            wax_id=wax_id,
            original_message=student_entry,
            now=now,
            consultation=consultation,
            prior_results=tuple(prior_results),
            round_number=self._max_consultations,
            max_rounds=self._max_consultations,
        )
        outcome = await self._teacher.teach(inp)
        reply_text = outcome.reply_text or ""
        return reply_text, consultations_made, self._summarize_context(prior_results)

    @staticmethod
    def _summarize_context(results: list) -> str:
        """Compact summary of CI results for the audit/duplicate path."""
        if not results:
            return "Teacher responded without consulting CI."
        return f"Teacher made {len(results)} consultation(s)."


__all__ = ["TurnResult", "TurnService"]
