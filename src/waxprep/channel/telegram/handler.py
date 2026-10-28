"""Telegram inbound message handler with streaming support.

This module provides the production-ready inbound processing pipeline for Telegram:
1. Resolve sender identity (ChannelIdentity -> WaxId)
2. Durably save the inbound message
3. Queue background processing (CI, Teacher, etc.)
4. Return success only after durable acceptance
5. Support streaming via native Telegram draft messages

The handler ensures:
- Every message is saved exactly once (idempotency via provider_message_id)
- Unknown senders receive a clear linking instruction
- Processing failures don't lose messages
- Streaming is supported via sendMessageDraft API
- No direct model calls in the webhook path
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from waxprep.channel.contracts import (
    CONTENT_KIND_INTERACTIVE_BUTTON,
    InteractiveSuggestion,
    NormalizedInbound,
    OutboundPayload,
)
from waxprep.channel.message_queue import MessageQueue, QueuedMessage
from waxprep.channel.streaming.accumulator import AnswerAccumulator
from waxprep.channel.streaming.contract import ChannelCapability
from waxprep.channel.streaming.coordinator import StreamingCoordinator
from waxprep.channel.streaming.display import ChoicesMarkerFilter
from waxprep.channel.streaming.observability import StreamingCounters
from waxprep.channel.streaming.pacing import TELEGRAM_DRAFT_DEFAULT
from waxprep.channel.streaming.renderers import telegram_capabilities
from waxprep.channel.telegram.client import TelegramClient
from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    InteractiveOffer,
    InteractiveOfferStatus,
    JobStatus,
    ObjectType,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.index.indexer import JOB_INGEST_MEDIA, JOB_PROCESS_TURN
from waxprep.orchestration.contracts_ci_first import TextDeltaSink
from waxprep.orchestration.turn_ci_first import TurnResultCiFirst, TurnServiceCiFirst
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramInboundConfig:
    """Configuration for Telegram inbound processing."""

    channel: str = "telegram"
    max_attempts: int = 3
    retry_base_delay: float = 1.0
    linking_instruction: str = (
        "Please link your Telegram account to your WAXPREP account first. "
        "Contact support to complete registration."
    )
    disabled_instruction: str = (
        "Your account is currently disabled. Please contact support."
    )
    # Minimum spacing between native draft-preview updates. Draft requests
    # are real API calls; throttling keeps a fast stream within Telegram's
    # rate limits. 0 disables throttling.
    draft_min_interval_seconds: float = 1.0
    # When a live final send fails, the outbound record is left retryable
    # and the durable outbox re-sends the stored authoritative reply after
    # this delay (with exponential backoff on later attempts).
    send_retry_delay_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class InboundResult:
    """Result of processing an inbound Telegram message."""

    accepted: bool
    wax_id: WaxId | None
    student_entry_id: UUID | None
    outbound_delivery_id: UUID | None
    needs_linking: bool = False
    error: str | None = None


class TelegramInboundHandler:
    """Production inbound Telegram message handler with streaming support.

    This handler implements the complete inbound pipeline:
    - Resolves sender identity via ChannelIdentity lookup
    - Durably saves the message before any processing
    - Connects to CI-first turn service for processing
    - Handles unknown, disabled, and malformed cases
    - Provides idempotency via (channel, provider_message_id)
    - Supports streaming via Telegram's native draft API

    The handler NEVER:
    - Makes model calls directly in the webhook path
    - Creates duplicate student records for unknown numbers
    - Silently drops messages
    - Returns success before durable acceptance
    """

    def __init__(
        self,
        storage: Storage,
        history: HistoryService,
        turn_service: TurnServiceCiFirst,
        clock: Clock,
        *,
        config: TelegramInboundConfig | None = None,
        telegram_client: TelegramClient | None = None,
        message_queue: MessageQueue | None = None,
    ) -> None:
        self._storage = storage
        self._history = history
        self._turn_service = turn_service
        self._clock = clock
        self._config = config or TelegramInboundConfig()
        self._telegram_client = telegram_client
        # Optional per-student queue coordinating rapid message bursts
        # into single tutor turns (time-window coalescing only).
        self._message_queue = message_queue
        self._message_locks: dict[str, asyncio.Lock] = {}
        self._message_locks_guard = asyncio.Lock()

        # Streaming support
        self._streaming_counters = StreamingCounters()
        self._capabilities = telegram_capabilities()

    async def _lock_for_message(self, provider_message_id: str) -> asyncio.Lock:
        async with self._message_locks_guard:
            lock = self._message_locks.get(provider_message_id)
            if lock is None:
                lock = asyncio.Lock()
                self._message_locks[provider_message_id] = lock
            return lock

    def resolve_sender(self, sender: str) -> WaxId | None:
        """Resolve a Telegram chat_id to a WAX ID."""
        identity = self._storage.get_channel_identity(self._config.channel, sender)
        if identity is not None:
            return identity.wax_id
        return None

    def _ensure_student_exists(self, wax_id: WaxId) -> bool:
        """Check if student exists and is active."""
        from waxprep.domain.models import StudentStatus

        student = self._storage.get_student(wax_id)
        if student is None:
            logger.warning(
                "resolved wax_id has no student record",
                extra={"wax_id": str(wax_id)},
            )
            return False
        if student.status == StudentStatus.DISABLED:
            logger.warning(
                "student account is disabled",
                extra={"wax_id": str(wax_id)},
            )
            return False
        return True

    async def handle_message(self, msg: NormalizedInbound) -> InboundResult:
        """Process a normalized inbound Telegram message."""
        mid = (msg.provider_message_id or "").strip()
        if mid:
            lock = await self._lock_for_message(mid)
            async with lock:
                return await self._handle_message_unlocked(msg)
        return await self._handle_message_unlocked(msg)

    async def _handle_message_unlocked(self, msg: NormalizedInbound) -> InboundResult:
        """Process a normalized inbound Telegram message (main logic)."""
        logger.info(
            "telegram inbound processing started",
            extra={
                "provider_message_id": msg.provider_message_id,
                "provider_sender_id": msg.provider_sender_id,
                "has_text": bool(msg.text),
                "has_attachments": len(msg.attachments) > 0,
            },
        )

        # Step 1: Resolve sender identity
        wax_id = self.resolve_sender(msg.provider_sender_id)

        if wax_id is None:
            logger.info(
                "unresolved telegram sender",
                extra={"sender": msg.provider_sender_id},
            )
            # Acknowledge button clicks even when the sender is unknown —
            # Telegram clients show a loading spinner until the callback
            # query is answered.
            if msg.content_kind == CONTENT_KIND_INTERACTIVE_BUTTON:
                await self._answer_callback(
                    self._callback_query_id(msg),
                    "Please link your Telegram account to your WAXPREP "
                    "account first.",
                )
            return InboundResult(
                accepted=True,
                wax_id=None,
                student_entry_id=None,
                outbound_delivery_id=None,
                needs_linking=True,
                error=None,
            )

        # Step 2: Check student status
        if not self._ensure_student_exists(wax_id):
            logger.warning(
                "student not found or disabled",
                extra={
                    "wax_id": str(wax_id),
                    "provider_sender_id": msg.provider_sender_id,
                },
            )
            if msg.content_kind == CONTENT_KIND_INTERACTIVE_BUTTON:
                await self._answer_callback(
                    self._callback_query_id(msg),
                    "Your account is currently disabled.",
                )
            return InboundResult(
                accepted=True,
                wax_id=wax_id,
                student_entry_id=None,
                outbound_delivery_id=None,
                needs_linking=False,
                error="student_disabled_or_not_found",
            )

        # Step 3: Check for duplicate delivery (idempotency by channel_ref)
        existing = self._storage.find_history_by_channel_ref(
            wax_id, self._config.channel, msg.provider_message_id
        )
        if existing is not None:
            logger.info(
                "duplicate telegram message detected",
                extra={
                    "provider_message_id": msg.provider_message_id,
                    "wax_id": str(wax_id),
                },
            )
            tutor_reply = self._storage.find_tutor_reply_to(wax_id, existing.id)
            if tutor_reply is not None:
                outbound = self._storage.get_outbound_delivery_for_tutor(
                    wax_id, tutor_reply.id, self._config.channel
                )
                return InboundResult(
                    accepted=True,
                    wax_id=wax_id,
                    student_entry_id=existing.id,
                    outbound_delivery_id=outbound.id if outbound else None,
                    needs_linking=False,
                    error=None,
                )

        # Step 3b: One-shot interactive button consume (inline keyboard).
        # Telegram keeps old inline buttons tappable forever; we reject
        # stale/already-used clicks at the channel layer and acknowledge
        # the callback query so the client never hangs on a spinner. The
        # callback payload (opaque button id) is NEVER treated as
        # authorization: the one-shot InteractiveOffer for THIS student is
        # the authorization fact, consumed with a CAS update.
        if msg.content_kind == CONTENT_KIND_INTERACTIVE_BUTTON:
            callback_query_id = self._callback_query_id(msg)
            button_id = str(msg.structured.get("button_id") or "")
            if not button_id:
                await self._answer_callback(callback_query_id, None)
                return InboundResult(
                    accepted=True,
                    wax_id=wax_id,
                    student_entry_id=None,
                    outbound_delivery_id=None,
                    needs_linking=False,
                    error="interactive_click_malformed",
                )
            offer = self._storage.find_interactive_offer_by_button_id(wax_id, button_id)
            if offer is None:
                logger.info(
                    "stale or unknown interactive click rejected",
                    extra={
                        "wax_id": str(wax_id),
                        "button_id": button_id,
                    },
                )
                await self._answer_callback(
                    callback_query_id, "This option is no longer available."
                )
                return InboundResult(
                    accepted=True,
                    wax_id=wax_id,
                    student_entry_id=None,
                    outbound_delivery_id=None,
                    needs_linking=False,
                    error="interactive_offer_unavailable",
                )
            consumed = self._storage.consume_interactive_offer(
                wax_id, offer.id, button_id, self._clock.now()
            )
            if consumed is None:
                logger.info(
                    "interactive offer already consumed or expired",
                    extra={
                        "wax_id": str(wax_id),
                        "offer_id": str(offer.id),
                        "button_id": button_id,
                    },
                )
                await self._answer_callback(
                    callback_query_id, "This option was already used."
                )
                return InboundResult(
                    accepted=True,
                    wax_id=wax_id,
                    student_entry_id=None,
                    outbound_delivery_id=None,
                    needs_linking=False,
                    error="interactive_offer_consumed",
                )
            # Resolve the human title for the opaque id so the click flows
            # through the normal durable pipeline as an ordinary student
            # turn ("Practice question" etc.), not as a platform event.
            title = ""
            for btn in offer.buttons:
                if str(btn.get("id")) == button_id:
                    title = str(btn.get("title") or "").strip()
                    break
            msg = replace(msg, text=title or "Selected option")
            # Acknowledge immediately (no text) — the answer arrives as the
            # tutor's streamed turn, not as a callback toast.
            await self._answer_callback(callback_query_id, None)

        # Step 4: Durably save the student message
        try:
            student_entry, created = self._history.record_with_status(
                wax_id,
                AuthorKind.STUDENT,
                msg.text or "",
                content_kind=msg.content_kind or "text",
                channel=self._config.channel,
                channel_ref=msg.provider_message_id,
                thread_ref=msg.reply_to_provider_id,
                occurred_at=msg.occurred_at,
            )

            logger.info(
                "student message durably saved",
                extra={
                    "wax_id": str(wax_id),
                    "entry_id": str(student_entry.id),
                    "created": created,
                },
            )

            # Non-blocking media derivation
            if created and msg.attachments:
                try:
                    media_now = self._clock.now()
                    self._storage.enqueue_job(
                        DerivationJob(
                            id=new_id(),
                            wax_id=wax_id,
                            kind=JOB_INGEST_MEDIA,
                            object_type=ObjectType.HISTORY,
                            object_id=student_entry.id,
                            status=JobStatus.PENDING,
                            attempts=0,
                            run_after=media_now,
                            created_at=media_now,
                            updated_at=media_now,
                        )
                    )
                except Exception:
                    logger.debug(
                        "failed to enqueue ingest_media job",
                        exc_info=True,
                    )

            if not created:
                existing_reply = self._storage.find_tutor_reply_to(
                    wax_id, student_entry.id
                )
                if existing_reply is not None:
                    outbound = self._storage.get_outbound_delivery_for_tutor(
                        wax_id, existing_reply.id, self._config.channel
                    )
                    return InboundResult(
                        accepted=True,
                        wax_id=wax_id,
                        student_entry_id=student_entry.id,
                        outbound_delivery_id=outbound.id if outbound else None,
                        needs_linking=False,
                        error=None,
                    )

        except Exception as exc:
            logger.exception(
                "failed to save student message",
                extra={
                    "wax_id": str(wax_id),
                    "provider_message_id": msg.provider_message_id,
                    "error": str(exc),
                },
            )
            return InboundResult(
                accepted=False,
                wax_id=wax_id,
                student_entry_id=None,
                outbound_delivery_id=None,
                needs_linking=False,
                error=f"failed_to_save_message: {type(exc).__name__}",
            )

        # Step 5: Process through CI-first turn service with streaming support.
        # Optional per-student queue: rapid bursts are coordinated into
        # single tutor turns (time-window coalescing only, never keyword
        # matching). The message is already durably saved.
        if self._message_queue is not None:
            queue_id = await self._message_queue.submit(
                msg,
                wax_id,
                entry_id=student_entry.id,
                channel=self._config.channel,
            )
            if queue_id is not None:
                logger.info(
                    "message submitted to per-student queue",
                    extra={
                        "wax_id": str(wax_id),
                        "queue_id": str(queue_id),
                        "provider_message_id": msg.provider_message_id,
                    },
                )
                return InboundResult(
                    accepted=True,
                    wax_id=wax_id,
                    student_entry_id=student_entry.id,
                    outbound_delivery_id=None,
                    needs_linking=False,
                    error=None,
                )
            # Queue declined (rate limited): process inline below.

        return await self._execute_turn(wax_id, msg, student_entry.id)

    async def run_queued_turn(
        self,
        wax_id: WaxId,
        batch: list[QueuedMessage],
    ) -> None:
        """Process one coalesced queue batch as a single tutor turn.

        Mirrors the WhatsApp handler: time-window batching only, exact
        messages stay saved verbatim, the merged text drives the CI
        investigation, and the trigger (newest) message anchors the
        turn. Draft streaming runs for the trigger's chat.
        """
        if not batch:
            return
        trigger = batch[-1]
        texts = [
            (item.message.text or "").strip()
            for item in batch
            if (item.message.text or "").strip()
        ]
        merged = "\n\n".join(texts) or (trigger.message.text or "")
        entry_id = trigger.entry_id
        if entry_id is None:
            existing = self._storage.find_history_by_channel_ref(
                wax_id, self._config.channel, trigger.message.provider_message_id
            )
            entry_id = existing.id if existing is not None else None
        await self._execute_turn(
            wax_id,
            trigger.message,
            entry_id,
            text=merged,
        )

    async def _execute_turn(
        self,
        wax_id: WaxId,
        msg: NormalizedInbound,
        student_entry_id: UUID | None,
        *,
        text: str | None = None,
    ) -> InboundResult:
        """Run the CI-first turn with streaming and deliver the reply."""
        turn_text = text if text is not None else (msg.text or "")
        recipient = msg.provider_sender_id
        on_text_delta: TextDeltaSink | None = None
        last_provider_message_id: str | None = None
        coordinator: StreamingCoordinator | None = None

        if self._telegram_client is not None:
            # Typing indicator at turn start (sendChatAction, best-effort).
            # The native draft preview (or the coordinator's complete
            # messages) takes over once generation starts.
            try:
                await self._telegram_client.send_typing(recipient)
            except Exception:
                logger.debug("turn-start typing action failed", exc_info=True)

            caps = self._capabilities
            supports_draft = caps.has(
                ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES
            ) or caps.has(ChannelCapability.NATIVE_RICH_DRAFT_UPDATES)

            if supports_draft:
                # Use native Telegram draft streaming (verified capability).
                # draft_id MUST be a non-zero integer per the Bot API;
                # reusing it animates the same preview.
                on_text_delta = self._create_draft_sink(
                    self._telegram_client,
                    recipient,
                    draft_id=uuid4().int & ((1 << 62) - 1) | 1,
                    min_interval_seconds=self._config.draft_min_interval_seconds,
                )
            else:
                # Fall back to progressive complete-message streaming via
                # the shared coordinator (pacing + stream-safe formatting).
                accumulator = AnswerAccumulator()
                accumulator.start()
                client = self._telegram_client

                async def send_update(text: str) -> str | None:
                    await self._send_progressive_chunk(client, recipient, text)
                    return None

                coord = StreamingCoordinator(
                    accumulator=accumulator,
                    capabilities=caps,
                    pacing=TELEGRAM_DRAFT_DEFAULT,
                    send_update=send_update,
                )
                coordinator = coord

                async def on_delta(text: str) -> None:
                    await coord.on_delta(text)

                on_text_delta = on_delta

        try:
            turn_result: TurnResultCiFirst = (
                await self._turn_service.handle_student_message(
                    wax_id=wax_id,
                    text=turn_text,
                    channel=self._config.channel,
                    channel_ref=msg.provider_message_id,
                    thread_ref=msg.reply_to_provider_id,
                    occurred_at=msg.occurred_at,
                    on_text_delta=on_text_delta,
                )
            )

            # Claim outbound before any channel send so the outbox worker
            # cannot deliver the same tutor text a second time.
            skip_live_send = False
            if turn_result.outbound_delivery_id is not None:
                claimed = self._storage.claim_outbound_delivery(
                    wax_id,
                    turn_result.outbound_delivery_id,
                    self._clock.now(),
                )
                if claimed is None:
                    # Already claimed/delivered by another path — skip send.
                    logger.info(
                        "outbound already claimed; skipping live send",
                        extra={
                            "outbound_delivery_id": str(
                                turn_result.outbound_delivery_id
                            )
                        },
                    )
                    skip_live_send = True

            # Send final message
            # delivery_ok gates the interactive issue's outbound bookkeeping:
            # when another replica owns live delivery (claim lost) or a
            # piece failed, the outbound record must stay retryable.
            delivery_ok = not skip_live_send
            if (
                turn_result.tutor_entry is not None
                and turn_result.tutor_entry.text
                and not skip_live_send
            ):
                final_text = turn_result.tutor_entry.text

                if coordinator is not None:
                    # Finalize the streamed response: the coordinator's
                    # pacing policy sends the final streamed update. The
                    # authoritative complete message is sent below.
                    await coordinator.complete()

                # Send the complete answer as coherent complete messages
                # (split only at the platform transport limit). Success is
                # tracked per piece: the outbound record is marked
                # delivered ONLY when the platform accepted the whole
                # answer, otherwise it is left retryable so the durable
                # outbox recovers it (never silently dropped).
                delivery_ok = False
                if self._telegram_client is not None:
                    sent_id = await self._send_final_text(wax_id, recipient, final_text)
                    delivery_ok = sent_id is not None
                    if sent_id is not None:
                        last_provider_message_id = sent_id

                if turn_result.outbound_delivery_id is not None:
                    if delivery_ok:
                        try:
                            self._storage.mark_outbound_delivered(
                                wax_id,
                                turn_result.outbound_delivery_id,
                                self._clock.now(),
                                provider_message_id=last_provider_message_id,
                            )
                        except Exception:
                            logger.debug(
                                "mark outbound delivered failed",
                                exc_info=True,
                            )
                    else:
                        # A live send failed. Leave the outbound record
                        # retryable: the durable outbox re-sends the
                        # stored authoritative reply after a short delay
                        # (with backoff), so the reply is never dropped.
                        try:
                            self._storage.mark_outbound_failed(
                                wax_id,
                                turn_result.outbound_delivery_id,
                                self._clock.now(),
                                error="live_delivery_failed",
                                retry_at=self._clock.now()
                                + timedelta(
                                    seconds=self._config.send_retry_delay_seconds
                                ),
                            )
                        except Exception:
                            logger.debug(
                                "mark outbound retryable failed",
                                exc_info=True,
                            )

            # Optional interactive presentation (AI-chosen). Opaque ids
            # only; one button per row; callback_data carries the opaque
            # id (well under Telegram's 64-byte callback_data limit).
            if (
                turn_result.interactive is not None
                and self._telegram_client is not None
                and not turn_result.duplicate
            ):
                await self._issue_interactive(
                    wax_id=wax_id,
                    recipient=recipient,
                    suggestion=turn_result.interactive,
                    tutor_entry_id=turn_result.tutor_entry.id
                    if turn_result.tutor_entry
                    else None,
                    outbound_delivery_id=turn_result.outbound_delivery_id,
                    body_delivered=delivery_ok,
                )

            logger.info(
                "turn processing completed",
                extra={
                    "wax_id": str(wax_id),
                    "student_entry_id": str(turn_result.student_entry.id),
                    "tutor_entry_id": str(turn_result.tutor_entry.id)
                    if turn_result.tutor_entry
                    else None,
                    "duplicate": turn_result.duplicate,
                    "outbound_delivery_id": turn_result.outbound_delivery_id,
                    "streaming": on_text_delta is not None,
                },
            )

            return InboundResult(
                accepted=True,
                wax_id=wax_id,
                student_entry_id=turn_result.student_entry.id,
                outbound_delivery_id=turn_result.outbound_delivery_id,
                needs_linking=False,
                error=None,
            )

        except Exception as exc:
            logger.exception(
                "failed during turn processing",
                extra={
                    "wax_id": str(wax_id),
                    "student_entry_id": str(student_entry_id),
                    "error": str(exc),
                },
            )
            # Persist a durable, independently retryable processing job
            now = self._clock.now()
            if student_entry_id is not None:
                try:
                    self._storage.enqueue_job(
                        DerivationJob(
                            id=new_id(),
                            wax_id=wax_id,
                            kind=JOB_PROCESS_TURN,
                            object_type=ObjectType.HISTORY,
                            object_id=student_entry_id,
                            status=JobStatus.PENDING,
                            attempts=0,
                            run_after=now,
                            created_at=now,
                            updated_at=now,
                            last_error=f"processing_failed: {type(exc).__name__}",
                        )
                    )
                except Exception:
                    logger.exception(
                        "failed to enqueue turn-processing recovery job",
                        extra={
                            "wax_id": str(wax_id),
                            "student_entry_id": str(student_entry_id),
                        },
                    )
            else:
                logger.error(
                    "cannot enqueue recovery job without a student entry id",
                    extra={"wax_id": str(wax_id), "error": str(exc)},
                )
            return InboundResult(
                accepted=True,
                wax_id=wax_id,
                student_entry_id=student_entry_id,
                outbound_delivery_id=None,
                needs_linking=False,
                error=f"processing_failed: {type(exc).__name__}",
            )

    def _callback_query_id(self, msg: NormalizedInbound) -> str:
        """Extract the callback query id from a normalized button click."""
        return str(msg.structured.get("callback_query_id") or "")

    async def _answer_callback(self, callback_query_id: str, text: str | None) -> None:
        """Best-effort callback acknowledgement (never raises)."""
        client = self._telegram_client
        if client is None or not callback_query_id:
            return
        try:
            await client.answer_callback_query(callback_query_id, text=text)
        except Exception:
            logger.debug("answer callback query failed", exc_info=True)

    def _create_draft_sink(
        self,
        client: TelegramClient,
        chat_id: str,
        *,
        draft_id: int,
        min_interval_seconds: float,
    ) -> TextDeltaSink:
        """Create a delta sink that updates a native Telegram draft preview.

        The sink ACCUMULATES display-safe text (the interactive-choices
        trailer is filtered out and never shown in the preview), throttles
        draft updates to ``min_interval_seconds`` (draft requests are real
        API calls), and clamps the preview to the API's 4096-character
        draft limit by showing the latest tail. Send failures never raise
        into the model stream — the final ``sendMessage`` is authoritative.
        """
        marker_filter = ChoicesMarkerFilter()
        parts: list[str] = []
        last_sent: datetime | None = None

        async def sink(delta: str) -> None:
            nonlocal last_sent
            safe = marker_filter.feed(delta)
            if safe:
                parts.append(safe)
            text = "".join(parts)[-4096:]
            if not text.strip():
                return
            now = self._clock.now()
            if (
                last_sent is not None
                and min_interval_seconds > 0
                and (now - last_sent).total_seconds() < min_interval_seconds
            ):
                return
            last_sent = now
            result = await client.send_message_draft(
                chat_id=chat_id,
                text=text,
                draft_id=draft_id,
            )
            if not result.success:
                logger.warning(
                    "telegram draft update failed",
                    extra={"error": result.error_detail},
                )
                self._streaming_counters.record_delta_failure()
            else:
                self._streaming_counters.record_delta_success()

        return sink

    async def _send_final_text(
        self,
        wax_id: WaxId,
        recipient: str,
        text: str,
    ) -> str | None:
        """Send the authoritative answer as complete Telegram messages.

        Splits only at the platform transport limit, sends every piece in
        order, and returns the LAST provider message id on full success.
        Returns None when any piece is rejected or raises — the caller then
        leaves the outbound record retryable so the durable outbox re-sends
        the stored reply (at-least-once, never silently dropped).
        """
        client = self._telegram_client
        if client is None:
            return None
        from waxprep.channel.delivery import split_for_platform

        last_provider_message_id: str | None = None
        try:
            for piece in split_for_platform(text, self._config.channel):
                result = await client.send_text(
                    OutboundPayload(
                        channel=self._config.channel,
                        wax_id=wax_id,
                        recipient_external_id=recipient,
                        text=piece,
                    )
                )
                if not result.success:
                    logger.warning(
                        "final send rejected by platform; outbox will recover",
                        extra={
                            "error_code": result.error_code,
                            "temporary": result.temporary_failure,
                        },
                    )
                    return None
                if result.provider_message_id:
                    last_provider_message_id = result.provider_message_id
        except Exception:
            logger.warning("final send raised; outbox will recover", exc_info=True)
            return None
        return last_provider_message_id

    async def _issue_interactive(
        self,
        *,
        wax_id: WaxId,
        recipient: str,
        suggestion: InteractiveSuggestion,
        tutor_entry_id: UUID | None,
        outbound_delivery_id: UUID | None,
        body_delivered: bool,
    ) -> None:
        """Send optional inline-keyboard buttons and register a one-shot offer.

        Opaque button ids only (well under Telegram's 64-byte
        callback_data limit), one button per row. The interactive payload
        is never an authorization fact: the stored one-shot
        InteractiveOffer for this student authorizes the click. When the
        body teaching was NOT delivered live (send failed or another
        replica owns delivery), the outbound record is left alone so the
        durable outbox still recovers the body.
        """
        client = self._telegram_client
        if client is None or not suggestion.buttons:
            return
        now = self._clock.now()
        issued: list[dict[str, str]] = []
        for btn in suggestion.buttons[:3]:
            title = (btn.title or "").strip()[:20]
            if not title:
                continue
            issued.append({"id": str(uuid4()), "title": title})
        if not issued:
            return
        body = (suggestion.body_text or "").strip() or "Choose a next step:"
        keyboard = {
            "inline_keyboard": [
                [{"text": btn["title"], "callback_data": btn["id"]}] for btn in issued
            ]
        }
        try:
            result = await client.send_text(
                OutboundPayload(
                    channel=self._config.channel,
                    wax_id=wax_id,
                    recipient_external_id=recipient,
                    text=body[:4096],
                ),
                reply_markup=keyboard,
            )
        except Exception:
            logger.debug("interactive send raised", exc_info=True)
            return
        if not result.success:
            logger.info(
                "interactive send failed; body teaching remains available",
                extra={
                    "wax_id": str(wax_id),
                    "error_code": result.error_code,
                    "temporary": result.temporary_failure,
                },
            )
            return
        try:
            self._storage.create_interactive_offer(
                InteractiveOffer(
                    id=new_id(),
                    wax_id=wax_id,
                    tutor_entry_id=tutor_entry_id,
                    provider_outbound_message_id=result.provider_message_id,
                    buttons=tuple(issued),
                    status=InteractiveOfferStatus.OPEN,
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(hours=48),
                )
            )
        except Exception:
            logger.exception(
                "failed to register interactive offer after successful send",
                extra={"wax_id": str(wax_id)},
            )
        if outbound_delivery_id is not None and result.provider_message_id:
            if not body_delivered:
                # The body was not delivered live — leave the outbound
                # record retryable so the outbox delivers the teaching.
                return
            try:
                self._storage.mark_outbound_delivered(
                    wax_id,
                    outbound_delivery_id,
                    now,
                    provider_message_id=result.provider_message_id,
                )
            except Exception:
                logger.debug("mark outbound after interactive failed", exc_info=True)

    async def _send_progressive_chunk(
        self, client: TelegramClient, chat_id: str, text: str
    ) -> None:
        """Send a progressive chunk as a complete message."""
        if not text.strip():
            return
        result = await client.send_text(
            OutboundPayload(
                channel=self._config.channel,
                wax_id=new_wax_id(),  # placeholder - the client ignores it
                recipient_external_id=chat_id,
                text=text,
            )
        )
        if not result.success:
            logger.warning(
                "telegram progressive chunk send failed",
                extra={"error": result.error_detail},
            )
            self._streaming_counters.record_delta_failure()
        else:
            self._streaming_counters.record_delta_success()

    def create_linking_response(self, sender: str) -> str:
        """Create a response for unlinked senders."""
        return self._config.linking_instruction

    def create_disabled_response(self) -> str:
        """Create a response for disabled accounts."""
        return self._config.disabled_instruction


__all__ = ["TelegramInboundHandler", "TelegramInboundConfig", "InboundResult"]
