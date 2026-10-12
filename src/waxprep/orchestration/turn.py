"""Teacher-first turn orchestration (DEPRECATED).

DEPRECATED: This architecture has been superseded by CI-first orchestration
in ``turn_ci_first.py``. See docs/ARCHITECTURE.md for the current design.

The old architecture was:

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

NEW ARCHITECTURE (use turn_ci_first.py):
    Student message -> Save -> CI (mandatory) -> Teacher -> Save reply -> Outbox
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
from waxprep.orchestration.bounded import BoundedConsultation
from waxprep.orchestration.consultation import CIBackedConsultation
from waxprep.orchestration.contracts import (
    Teacher,
    TeacherConsultation,
    TeacherInput,
    TeacherOutcome,
)
from waxprep.orchestration.errors import InvalidTeacherOutcomeError
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
    """Teacher-first turn orchestrator (DEPRECATED).

    DEPRECATED: Use TurnServiceCiFirst from turn_ci_first.py instead.

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
                # The reply exists. Check whether the outbound delivery
                # record also exists. If a crash occurred between saving
                # the reply and creating the outbox record, the record
                # is missing — repair it now without invoking the Teacher.
                outbound = self._storage.get_outbound_delivery_for_tutor(
                    wax_id, existing_reply.id, channel
                )
                outbound_id = outbound.id if outbound is not None else None
                if outbound is None and channel is not None:
                    # Repair: create the missing outbox record for the
                    # existing tutor reply. This does NOT invoke the
                    # Teacher — it reuses the stored reply.
                    now = self._clock.now()
                    repaired = self._storage.create_outbound_delivery(
                        OutboundDelivery(
                            id=new_id(),
                            wax_id=wax_id,
                            tutor_entry_id=existing_reply.id,
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
                    outbound_id = repaired.id
                return TurnResult(
                    student_entry=student_entry,
                    tutor_entry=existing_reply,
                    context_summary="Duplicate delivery; tutor reply already stored.",
                    duplicate=True,
                    consultations_made=0,
                    outbound_delivery_id=outbound_id,
                )
            # Previous attempt crashed before tutor reply was saved;
            # resume teaching below.

        # 3. Teacher drives the turn with bounded consultations.
        # The budget is enforced at the consultation-capability boundary:
        # every CI call across the entire turn is counted, including
        # multiple calls within one Teacher.teach() invocation.
        inner_consultation: TeacherConsultation = CIBackedConsultation(
            self._investigator,
            storage=self._storage,
            context_model_name=self._context_model_name,
        )
        consultation = BoundedConsultation(
            inner_consultation,
            max_consultations=self._max_consultations,
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

        # 4+5. Save exactly one tutor reply AND its outbound delivery
        # record in ONE transaction. A crash between them is repaired
        # on the next retry (step 2 above).
        now = self._clock.now()
        with self._storage.transaction():
            tutor_entry = self._history.record(
                wax_id,
                AuthorKind.TUTOR,
                reply_text,
                channel=channel,
                thread_ref=thread_ref,
                reply_to_id=student_entry.id,
            )
            outbound_id = None
            if channel is not None:
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
        consultation: BoundedConsultation,
    ) -> tuple[str | None, int, str]:
        """Run the Teacher with bounded consultations.

        The budget is enforced by the ``BoundedConsultation`` wrapper —
        every ``consult()`` call is counted. When the budget is
        exhausted, further calls return ``budget_exhausted`` and the
        Teacher must finish with the context it already has.

        Returns ``(reply_text, consultations_made, context_summary)``.
        ``reply_text`` is ``None`` if the Teacher declined to respond.

        Raises ``InvalidTeacherOutcomeError`` when the Teacher returns a
        blank final reply, or an unfinished outcome in the forced final
        round. Nothing invalid is ever persisted as a tutor reply.
        """
        prior_results: list = []
        last_summary = ""
        # Run up to max_consultations Teacher rounds. The BoundedConsultation
        # enforces the hard limit on actual CI calls, so even if the
        # Teacher calls consult() multiple times in one round, the total
        # is bounded.
        max_rounds = self._max_consultations
        for round_number in range(max_rounds):
            now = self._clock.now()
            inp = TeacherInput(
                wax_id=wax_id,
                original_message=student_entry,
                now=now,
                consultation=consultation,
                prior_results=tuple(prior_results),
                round_number=round_number,
                max_rounds=max_rounds,
            )
            outcome: TeacherOutcome = await self._teacher.teach(inp)
            prior_results = list(outcome.context_used)
            if outcome.finished:
                if outcome.reply_text is None:
                    return None, consultation.calls_made, last_summary
                reply_text = self._validated_reply_text(outcome.reply_text)
                last_summary = self._summarize_context(prior_results)
                return reply_text, consultation.calls_made, last_summary
            last_summary = self._summarize_context(prior_results)
            # If the budget is exhausted, force one final Teacher round
            # to produce a reply.
            if consultation.budget_exhausted:
                break
        # Final forced round: the Teacher must reply now.
        now = self._clock.now()
        inp = TeacherInput(
            wax_id=wax_id,
            original_message=student_entry,
            now=now,
            consultation=consultation,
            prior_results=tuple(prior_results),
            round_number=max_rounds,
            max_rounds=max_rounds,
        )
        outcome = await self._teacher.teach(inp)
        prior_results = list(outcome.context_used)
        # Validate the whole outcome, not just the text: an unfinished
        # outcome or a missing reply in the forced round is a controlled
        # failure. It must never be coerced into an empty string and
        # persisted as a successful tutor reply.
        if not outcome.finished or outcome.reply_text is None:
            logger.warning(
                "forced final teacher round produced no valid reply (finished=%s)",
                outcome.finished,
            )
            raise InvalidTeacherOutcomeError(
                "the teacher did not produce a valid final reply in the "
                "forced final round"
            )
        reply_text = self._validated_reply_text(outcome.reply_text)
        return (
            reply_text,
            consultation.calls_made,
            self._summarize_context(prior_results),
        )

    @staticmethod
    def _validated_reply_text(reply_text: str) -> str:
        """A successful final response must be a string with non-whitespace
        content. An empty or whitespace-only reply is never persisted."""
        if not reply_text.strip():
            logger.warning("teacher returned a blank final reply; refusing it")
            raise InvalidTeacherOutcomeError(
                "the teacher's final reply contains no usable text"
            )
        return reply_text

    @staticmethod
    def _summarize_context(results: list) -> str:
        """Compact summary of CI results for the audit/duplicate path."""
        if not results:
            return "Teacher responded without consulting CI."
        return f"Teacher made {len(results)} consultation(s)."


__all__ = ["TurnResult", "TurnService"]
