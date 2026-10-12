"""CI-first turn orchestration.

The architecture is:

    Student message arrives
        |
        v
    Save exact student message (atomic with index jobs)
        |
        v
    Context Intelligence (mandatory for every message)
        |
        v
    CI produces ContextPackage
        |
        v
    Teacher receives: student message + ContextPackage
        |
        v
    Teacher produces final educational response
        |
        v
    Save exactly one tutor reply (linked to student entry)
        |
        v
    Create outbound delivery record (outbox)
        |
        v
    (Channel adapter delivers; on crash, retry sends the same reply)

Key differences from Teacher-first:
- CI is mandatory for every message, not optional
- Teacher does NOT decide whether to consult CI
- Teacher does NOT generate consultation questions
- Teacher receives a complete ContextPackage, not raw CI results
- Teacher has one job: produce the final student-facing response
- No bounded consultation loop - CI runs once per turn with its own internal bounds
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from waxprep.clock import Clock
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.models import (
    ContextPackage,
    InvestigationLimits,
    InvestigationRequest,
)
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    OutboundDelivery,
    OutboundStatus,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.orchestration.contracts_ci_first import (
    Teacher as TeacherProtocol,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput as TeacherInputCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherOutcome as TeacherOutcomeCiFirst,
)
from waxprep.orchestration.errors import InvalidTeacherOutcomeError
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnResultCiFirst:
    """Outcome of a student turn with CI-first architecture."""

    student_entry: HistoryEntry
    tutor_entry: HistoryEntry | None
    context_package: ContextPackage | None
    duplicate: bool
    outbound_delivery_id: Any | None = None


class TurnServiceCiFirst:
    """CI-first turn orchestrator.

    CI runs automatically for every student message. The Teacher receives
    the student's exact message plus a complete ContextPackage and
    produces the final educational response in one round.

    The orchestrator enforces:
    - Student message is saved before any AI processing
    - CI investigation is bounded by InvestigationLimits
    - Teacher produces exactly one final response
    - Exactly one tutor reply is saved per student message
    """

    def __init__(
        self,
        storage: Storage,
        history: HistoryService,
        investigator: ContextInvestigator,
        teacher: TeacherProtocol,
        clock: Clock,
        *,
        ci_limits: InvestigationLimits | None = None,
        context_model_name: str = "context",
    ) -> None:
        self._storage = storage
        self._history = history
        self._investigator = investigator
        self._teacher = teacher
        self._clock = clock
        self._ci_limits = ci_limits or InvestigationLimits()
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
    ) -> TurnResultCiFirst:
        """Process one student turn end-to-end with CI-first architecture.

        Order of operations:
        1. Save the exact student message (atomic with index jobs).
           If the DB fails here, the channel adapter must allow a
           provider retry - no acknowledgment.
        2. Duplicate channel delivery: if a tutor reply already exists,
           return it without re-teaching. If the previous attempt
           crashed before the reply was saved, resume teaching.
        3. Context Intelligence runs automatically (mandatory stage).
        4. Teacher receives student message + ContextPackage, produces
           final response.
        5. Save exactly one tutor reply (linked to student entry).
        6. Create outbound delivery record (outbox).
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
                # the reply and creating the outbox record, repair it now.
                outbound = self._storage.get_outbound_delivery_for_tutor(
                    wax_id, existing_reply.id, channel
                )
                outbound_id = outbound.id if outbound is not None else None
                if outbound is None and channel is not None:
                    # Repair: create the missing outbox record for the
                    # existing tutor reply. This does NOT invoke the
                    # Teacher - it reuses the stored reply.
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
                return TurnResultCiFirst(
                    student_entry=student_entry,
                    tutor_entry=existing_reply,
                    context_package=None,
                    duplicate=True,
                    outbound_delivery_id=outbound_id,
                )
            # Previous attempt crashed before tutor reply was saved;
            # resume teaching below.

        # 3. Context Intelligence runs automatically for every message.
        now = self._clock.now()
        ci_request = InvestigationRequest(
            wax_id=wax_id,
            query=text,
            now=now,
            trigger_entry_id=student_entry.id,
            limits=self._ci_limits,
        )
        context_package: ContextPackage = await self._investigator.investigate(
            ci_request
        )

        # 4. Teacher receives student message + ContextPackage, produces final response.
        teacher_input = TeacherInputCiFirst(
            wax_id=wax_id,
            original_message=student_entry,
            context_package=context_package,
            now=now,
        )
        outcome: TeacherOutcomeCiFirst = await self._teacher.teach(teacher_input)

        # Handle empty Teacher response explicitly.
        # A blank or None reply is a controlled failure, not a silent completion.
        if outcome.reply_text is None:
            logger.warning(
                "Teacher returned None for wax_id=%s, message_id=%s; "
                "this is a controlled failure, not a valid turn completion",
                wax_id,
                student_entry.id,
            )
            raise InvalidTeacherOutcomeError(
                "Teacher returned None (no reply) for a processed message"
            )

        # Validate that the reply is not blank/whitespace
        reply_text = self._validated_reply_text(outcome.reply_text)

        # 5+6. Save exactly one tutor reply AND its outbound delivery
        # record in ONE transaction. A crash between them is repaired
        # on the next retry (step 2 above).
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

        return TurnResultCiFirst(
            student_entry=student_entry,
            tutor_entry=tutor_entry,
            context_package=context_package,
            duplicate=not created,
            outbound_delivery_id=outbound_id,
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


__all__ = ["TurnResultCiFirst", "TurnServiceCiFirst"]
