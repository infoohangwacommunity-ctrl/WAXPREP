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
from uuid import UUID

from waxprep.channel.contracts import InteractiveSuggestion
from waxprep.clock import Clock
from waxprep.context.exchange import trim_exchange
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.models import (
    ContextPackage,
    ExchangeTurn,
    InvestigationLimits,
    InvestigationRequest,
)
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    HistoryEntry,
    InvestigationRecord,
    OutboundDelivery,
    OutboundStatus,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.judge.service import QualityJudgeService
from waxprep.orchestration.contracts_ci_first import (
    Teacher as TeacherProtocol,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput as TeacherInputCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherOutcome as TeacherOutcomeCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TextDeltaSink,
)
from waxprep.orchestration.errors import InvalidTeacherOutcomeError
from waxprep.orchestration.latency import (
    STAGE_CI_END,
    STAGE_CI_START,
    STAGE_FIRST_VISIBLE,
    STAGE_REPLY_SAVED,
    STAGE_TEACHER_FIRST_DELTA,
    TurnLatencyRecord,
    TurnLatencyRecorder,
)
from waxprep.orchestration.post_turn import PostTurnQualityRecord, PostTurnReview
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
    interactive: InteractiveSuggestion | None = None


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
        judge: QualityJudgeService | None = None,
        latency_recorder: TurnLatencyRecorder | None = None,
    ) -> None:
        self._storage = storage
        self._history = history
        self._investigator = investigator
        self._teacher = teacher
        self._clock = clock
        self._ci_limits = ci_limits or InvestigationLimits()
        self._context_model_name = context_model_name
        self._judge = judge
        self._latency = latency_recorder

    async def handle_student_message(
        self,
        wax_id: WaxId,
        text: str,
        *,
        channel: str | None = None,
        channel_ref: str | None = None,
        thread_ref: str | None = None,
        occurred_at: datetime | None = None,
        on_text_delta: TextDeltaSink | None = None,
        live_delivery: bool = False,
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
           final response. Optional on_text_delta receives stream deltas
           for progressive channel delivery (infrastructure only).
        5. Save exactly one tutor reply (linked to student entry).
        6. Create outbound delivery record (outbox).

        Double-message race fix (founder's brief, "Double messages"):
        when ``live_delivery=True`` the outbound is created in
        ``IN_PROGRESS`` state (not ``PENDING``) inside the same
        transaction that saves the tutor reply. The outbox worker's
        ``claim_outbound_delivery`` CAS only transitions
        ``PENDING → IN_PROGRESS``, so it CANNOT claim a live-owned
        row. This closes the race window where the outbox would claim
        between (outbound created) and (live path's claim attempt)
        while progressive chunks were already being sent during
        streaming. The caller is then responsible for either
        ``mark_outbound_delivered`` (success) or
        ``mark_outbound_failed`` with ``retry_at`` set (release back
        to ``PENDING`` so the outbox retries).
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

        # Per-turn timing notes ("notes of time"): best-effort, never
        # blocks or fails the turn. Only turns that actually teach get
        # an event; duplicate redeliveries are cheap no-ops and skip.
        record: TurnLatencyRecord | None = None
        if self._latency is not None:
            record = self._latency.start(
                wax_id, channel=channel, trigger_entry_id=student_entry.id
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

        # 3. Race barrier: concurrent webhook + process_turn must not teach twice.
        existing_reply = self._storage.find_tutor_reply_to(wax_id, student_entry.id)
        if existing_reply is not None:
            outbound = self._storage.get_outbound_delivery_for_tutor(
                wax_id, existing_reply.id, channel
            )
            return TurnResultCiFirst(
                student_entry=student_entry,
                tutor_entry=existing_reply,
                context_package=None,
                duplicate=True,
                outbound_delivery_id=outbound.id if outbound else None,
            )

        # Cross-replica advisory lock (Postgres) / in-process set (memory).
        lock_held = self._storage.try_acquire_turn_lock(wax_id, student_entry.id)
        if not lock_held:
            import asyncio

            for _ in range(40):
                existing_reply = self._storage.find_tutor_reply_to(
                    wax_id, student_entry.id
                )
                if existing_reply is not None:
                    outbound = self._storage.get_outbound_delivery_for_tutor(
                        wax_id, existing_reply.id, channel
                    )
                    return TurnResultCiFirst(
                        student_entry=student_entry,
                        tutor_entry=existing_reply,
                        context_package=None,
                        duplicate=True,
                        outbound_delivery_id=outbound.id if outbound else None,
                    )
                await asyncio.sleep(0.05)
                lock_held = self._storage.try_acquire_turn_lock(
                    wax_id, student_entry.id
                )
                if lock_held:
                    break
            if not lock_held:
                existing_reply = self._storage.find_tutor_reply_to(
                    wax_id, student_entry.id
                )
                if existing_reply is not None:
                    outbound = self._storage.get_outbound_delivery_for_tutor(
                        wax_id, existing_reply.id, channel
                    )
                    return TurnResultCiFirst(
                        student_entry=student_entry,
                        tutor_entry=existing_reply,
                        context_package=None,
                        duplicate=True,
                        outbound_delivery_id=outbound.id if outbound else None,
                    )
                raise RuntimeError(
                    "turn lock unavailable; concurrent processing in progress"
                )

        try:
            # 4. Context Intelligence runs automatically for every message.
            now = self._clock.now()
            # The application hands CI the raw exchange material and
            # nothing more: no relevance judgement, no filtering, no
            # "this turn does not look interesting" rule. Supply only.
            exchange = self._collect_exchange(wax_id, student_entry.id)
            ci_request = InvestigationRequest(
                wax_id=wax_id,
                query=text,
                now=now,
                trigger_entry_id=student_entry.id,
                limits=self._ci_limits,
                exchange=exchange,
            )
            if record is not None:
                record.mark(STAGE_CI_START)
            context_package: ContextPackage = await self._investigator.investigate(
                ci_request
            )
            if record is not None:
                record.mark(STAGE_CI_END)
                if context_package is not None:
                    # investigation actions; tool calls are not
                    # separately reported by the package today.
                    record.ci_steps = len(context_package.investigated)

            # Persist the investigation audit row. In CI-first mode this is
            # the *only* audit trail for what CI did and what package it
            # produced. The audit row carries the closed-vocabulary
            # investigation_status and the invalid_selection flag so an
            # operator can distinguish a successful investigation from one
            # that hit a budget, had tools fail, or returned an invalid
            # evidence selection. The audit row is best-effort: a failure to
            # write it must not crash the turn (the student still gets an
            # answer), but it is logged loudly.
            self._record_investigation_audit(
                wax_id=wax_id,
                student_entry=student_entry,
                package=context_package,
                now=now,
            )

            # 4. Teacher receives student message + ContextPackage, produces final response.
            logger.info(
                "ci_investigation_complete",
                extra={
                    "wax_id": str(wax_id),
                    "student_entry_id": str(student_entry.id),
                    "evidence_count": len(context_package.evidence)
                    if context_package is not None
                    else 0,
                    "has_summary": bool(
                        context_package.summary if context_package else None
                    ),
                    "uncertainty_count": len(context_package.uncertainty)
                    if context_package and context_package.uncertainty
                    else 0,
                    "investigation_status": context_package.investigation_status.value,
                    "invalid_selection": context_package.invalid_selection,
                },
            )
            teacher_input = TeacherInputCiFirst(
                wax_id=wax_id,
                original_message=student_entry,
                context_package=context_package,
                now=now,
                tutor_guidance=(
                    self._judge.current_tutor_guidance(wax_id)
                    if self._judge is not None
                    else None
                ),
            )
            outcome: TeacherOutcomeCiFirst = await self._teacher.teach(
                teacher_input,
                on_text_delta=self._tracked_delta(on_text_delta, record),
            )

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
            logger.info(
                "tutor_reply_ready",
                extra={
                    "wax_id": str(wax_id),
                    "student_entry_id": str(student_entry.id),
                    "reply_chars": len(outcome.reply_text or ""),
                },
            )
            with self._storage.transaction():
                # Idempotent: a concurrent recovery path may have already saved
                # the tutor reply for this student entry.
                existing_tutor = self._storage.find_tutor_reply_to(
                    wax_id, student_entry.id
                )
                if existing_tutor is not None:
                    tutor_entry = existing_tutor
                else:
                    # Idempotent under one-tutor-reply-per-trigger unique index.
                    tutor_entry, _tutor_created = self._history.record_with_status(
                        wax_id,
                        AuthorKind.TUTOR,
                        reply_text,
                        channel=channel,
                        thread_ref=thread_ref,
                        reply_to_id=student_entry.id,
                    )
                outbound_id = None
                if channel is not None:
                    existing_out = self._storage.get_outbound_delivery_for_tutor(
                        wax_id, tutor_entry.id, channel
                    )
                    if existing_out is not None:
                        outbound_id = existing_out.id
                    else:
                        try:
                            # Double-message race fix (founder's brief):
                            # when ``live_delivery=True`` the outbound is
                            # created in ``IN_PROGRESS`` state directly,
                            # so the outbox worker cannot claim it while
                            # progressive chunks are being delivered. The
                            # caller is then responsible for either
                            # ``mark_outbound_delivered`` (success) or
                            # ``mark_outbound_failed`` with ``retry_at``
                            # (release back to ``PENDING`` so the outbox
                            # retries). When ``live_delivery=False`` the
                            # outbound is created ``PENDING`` as before —
                            # the durable outbox (or a live caller that
                            # explicitly claims afterward) is the delivery
                            # owner.
                            initial_status = (
                                OutboundStatus.IN_PROGRESS
                                if live_delivery
                                else OutboundStatus.PENDING
                            )
                            delivery = self._storage.create_outbound_delivery(
                                OutboundDelivery(
                                    id=new_id(),
                                    wax_id=wax_id,
                                    tutor_entry_id=tutor_entry.id,
                                    channel=channel,
                                    channel_ref=channel_ref,
                                    provider_message_id=None,
                                    status=initial_status,
                                    attempts=0,
                                    last_error=None,
                                    delivered_at=None,
                                    run_after=now,
                                    created_at=now,
                                    updated_at=now,
                                )
                            )
                            outbound_id = delivery.id
                        except Exception:
                            logger.exception(
                                "outbound delivery create failed; continuing with reply saved"
                            )
                            repaired = self._storage.get_outbound_delivery_for_tutor(
                                wax_id, tutor_entry.id, channel
                            )
                            outbound_id = repaired.id if repaired else None

            if record is not None:
                record.mark(STAGE_REPLY_SAVED)

            # Post-response quality record (structured event, no plaintext body).
            try:
                investigated = (
                    list(context_package.investigated) if context_package else []
                )
                notebook_ok = any(
                    x.startswith("proposal_accepted:") for x in investigated
                )
                PostTurnReview(self._storage, enable_stewardship=False).record_quality(
                    PostTurnQualityRecord(
                        wax_id=wax_id,
                        student_entry_id=student_entry.id,
                        tutor_entry_id=tutor_entry.id,
                        investigation_status=(
                            context_package.investigation_status.value
                            if context_package
                            else "unknown"
                        ),
                        evidence_count=(
                            len(context_package.evidence) if context_package else 0
                        ),
                        invalid_selection=bool(
                            context_package.invalid_selection
                            if context_package
                            else False
                        ),
                        notebook_proposal_accepted=notebook_ok,
                        reply_chars=len(reply_text or ""),
                        exchange_included=bool(context_package.exchange)
                        if context_package
                        else False,
                        exchange_turns=len(context_package.exchange)
                        if context_package
                        else 0,
                        notes=tuple(
                            x for x in investigated if x.startswith("proposal_")
                        )[:6],
                    ),
                    now=now,
                )
            except Exception:
                logger.exception("post-turn quality recording failed")

            # Quality Judge scheduling (cheap, no model call here): once
            # enough completed turns have accumulated since the last
            # assessment, enqueue one background evaluation job. The
            # judge runs in the worker; a scheduling failure must never
            # break the turn that already succeeded.
            if self._judge is not None:
                try:
                    self._judge.maybe_schedule(wax_id, now=now)
                except Exception:
                    logger.exception("quality judge scheduling failed")

            return TurnResultCiFirst(
                student_entry=student_entry,
                tutor_entry=tutor_entry,
                context_package=context_package,
                duplicate=not created,
                outbound_delivery_id=outbound_id,
                interactive=outcome.interactive,
            )

        finally:
            if lock_held:
                self._storage.release_turn_lock(wax_id, student_entry.id)
            if record is not None and self._latency is not None:
                # Notes of time: flush best-effort after the student's
                # work is done; a failure here must never break the
                # turn that already succeeded.
                self._latency.flush(record)

    def _tracked_delta(
        self, on_text_delta: TextDeltaSink | None, record: TurnLatencyRecord | None
    ) -> TextDeltaSink | None:
        """Wrap the channel delta sink to mark first-token timing notes.

        The first streamed delta marks the teacher's first token; the
        first delta the channel sink accepts without raising marks the
        first text the student can actually see.
        """

        if on_text_delta is None:
            return None
        sink = on_text_delta

        async def _sink(text: str) -> None:
            if record is not None:
                record.mark(STAGE_TEACHER_FIRST_DELTA)
            await sink(text)
            if record is not None:
                record.mark(STAGE_FIRST_VISIBLE)

        return _sink

    async def process_turn_job(self, job: DerivationJob) -> TurnResultCiFirst:
        """Recover a saved student message by re-running the full CI-first path.

        Idempotent: if a tutor reply already exists for the student entry,
        do not re-teach — return the existing outcome so outbox can finish.
        The exact student text is loaded from history and never altered.
        """
        wax_id = job.wax_id
        student_entry = self._storage.get_history_entry(wax_id, job.object_id)
        if student_entry is None:
            raise ValueError("process_turn: student history entry not found")

        existing = self._storage.find_tutor_reply_to(wax_id, student_entry.id)
        if existing is not None:
            outbound = self._storage.get_outbound_delivery_for_tutor(
                wax_id, existing.id, student_entry.channel
            )
            logger.info(
                "process_turn recovery: tutor reply already exists",
                extra={
                    "wax_id": str(wax_id),
                    "student_entry_id": str(student_entry.id),
                    "tutor_entry_id": str(existing.id),
                },
            )
            return TurnResultCiFirst(
                student_entry=student_entry,
                tutor_entry=existing,
                context_package=None,
                duplicate=True,
                outbound_delivery_id=outbound.id if outbound else None,
            )

        # Re-run full CI-first path with the exact saved student text.
        return await self.handle_student_message(
            wax_id,
            student_entry.text or "",
            channel=student_entry.channel,
            channel_ref=student_entry.channel_ref,
            thread_ref=student_entry.thread_ref,
            occurred_at=student_entry.occurred_at,
        )

    def _collect_exchange(
        self,
        wax_id: WaxId,
        trigger_entry_id: UUID,
    ) -> tuple[ExchangeTurn, ...]:
        """Assemble the raw exchange material for this turn.

        Mechanical, and deliberately so. The application takes the newest
        saved history entries, drops the message that triggered this
        turn (the Teacher renders that one separately), drops system
        noise and empty messages, puts the rest in chronological order,
        and trims it to the configured character budget.

        It does **not** decide that a turn matters, and it does not
        decide that one does not. That judgement belongs to Context
        Intelligence, and it is made on the assembled exchange — which
        is why the whole exchange is handed over, not a pre-filtered
        slice of it.

        Memory redesign note (founder's brief: "confused at 30-50
        messages"): the previous implementation capped the exchange at
        ``max_exchange_entries`` (12 turns), which silently hid older
        turns from the Teacher. That cap is GONE. The scan now uses
        ``max_history_scan_rows`` as a *DB round-trip protection*: a
        single page of recent history. When the page is full, the
        runtime records an ``exchange_scan_truncated`` flag in the audit
        trail, and CI can still walk further back via the
        ``list_history_paginated`` / ``search_history_by_keyword`` /
        ``search_history_by_time_range`` retrieval tools. The character
        budget (``max_exchange_characters``) is the ONLY trimming that
        reaches the Teacher — it shapes what fits in the model's
        context window, never what is relevant.
        """
        limits = self._ci_limits
        # DB-round-trip protection, NOT a relevance rule. The +1 lets us
        # detect truncation: if we got more rows than the budget, the
        # student's history extends past this page and CI may want to
        # paginate further back via list_history_paginated.
        scan_rows = max(limits.max_history_scan_rows, 1)
        entries = self._storage.list_history(
            wax_id,
            # One extra slot: the trigger entry is normally the newest
            # and is removed below. When the page returns scan_rows+1
            # rows, the scan truncated and older history exists.
            limit=scan_rows + 1,
            newest_first=True,
        )
        exchange_scan_truncated = len(entries) > scan_rows
        if exchange_scan_truncated:
            logger.info(
                "exchange_scan_truncated",
                extra={
                    "wax_id": str(wax_id),
                    "scan_rows": scan_rows,
                    "returned_rows": len(entries),
                    "hint": "CI may paginate further via list_history_paginated",
                },
            )
        turns: list[ExchangeTurn] = []
        for entry in entries:
            if entry.id == trigger_entry_id:
                continue
            if entry.author_kind == AuthorKind.SYSTEM:
                continue
            text = (entry.text or "").strip()
            if not text:
                continue
            turns.append(
                ExchangeTurn(
                    author_kind=entry.author_kind,
                    text=text,
                    entry_id=entry.id,
                    occurred_at=entry.occurred_at,
                )
            )
        # list_history returned newest-first; the exchange reads forward.
        turns.reverse()
        trimmed = trim_exchange(
            tuple(turns),
            max_characters=limits.max_exchange_characters,
        )
        # Surface the truncation flag through the application log so an
        # operator can see when a student's history outgrew one page.
        # The character budget is the ONLY trimming that reaches the
        # Teacher — it is a display budget, not a relevance rule.
        if exchange_scan_truncated:
            logger.debug(
                "exchange_scan_truncated_trimmed_to_char_budget",
                extra={
                    "wax_id": str(wax_id),
                    "raw_turns": len(turns),
                    "trimmed_turns": len(trimmed),
                },
            )
        return trimmed

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

    def _record_investigation_audit(
        self,
        *,
        wax_id: WaxId,
        student_entry: HistoryEntry,
        package: ContextPackage,
        now: datetime,
    ) -> None:
        """Persist one Context Intelligence audit row in CI-first mode.

        The audit row records the trigger entry, the input text, the
        investigation status, the package's contents, and any
        invalid-selection flag. It is best-effort: a failure to write the
        audit row must not prevent the student from getting an answer.
        The failure is logged so an operator can investigate.

        The row carries no plaintext student content beyond the input
        text the student themselves produced (``package.query``) — the
        evidence ids are references, not content. The summary and
        uncertainty are CI's own synthesis and are useful for
        debugging. The encryption-at-rest applied by the storage layer
        covers any sensitive fields.
        """
        try:
            self._storage.record_investigation(
                InvestigationRecord(
                    id=new_id(),
                    wax_id=wax_id,
                    trigger_entry_id=student_entry.id,
                    input_text=package.query,
                    result={
                        "investigation_status": package.investigation_status.value,
                        "summary": package.summary,
                        "evidence_ids": [str(e.id) for e in package.evidence],
                        "evidence_types": [
                            e.evidence_type.value for e in package.evidence
                        ],
                        "uncertainty": list(package.uncertainty),
                        "investigated": list(package.investigated),
                        "stopped_reason": package.stopped_reason,
                        "invalid_selection": package.invalid_selection,
                        "context_model": self._context_model_name,
                    },
                    model=self._context_model_name,
                    created_at=now,
                )
            )
        except Exception:
            logger.exception(
                "could not record CI investigation audit",
                extra={
                    "wax_id": str(wax_id),
                    "student_entry_id": str(student_entry.id),
                    "investigation_status": package.investigation_status.value,
                },
            )


__all__ = ["TurnResultCiFirst", "TurnServiceCiFirst"]
