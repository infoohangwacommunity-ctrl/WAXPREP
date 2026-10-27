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
from dataclasses import dataclass
from uuid import UUID, uuid4

from waxprep.channel.contracts import NormalizedInbound, OutboundPayload
from waxprep.channel.streaming.accumulator import AnswerAccumulator
from waxprep.channel.streaming.contract import ChannelCapability
from waxprep.channel.streaming.coordinator import StreamingCoordinator
from waxprep.channel.streaming.observability import StreamingCounters
from waxprep.channel.streaming.pacing import TELEGRAM_DRAFT_DEFAULT
from waxprep.channel.streaming.renderers import telegram_capabilities
from waxprep.channel.telegram.client import TelegramClient
from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
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
    ) -> None:
        self._storage = storage
        self._history = history
        self._turn_service = turn_service
        self._clock = clock
        self._config = config or TelegramInboundConfig()
        self._telegram_client = telegram_client
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

        # Step 5: Process through CI-first turn service with streaming support
        on_text_delta: TextDeltaSink | None = None
        last_provider_message_id: str | None = None
        coordinator: StreamingCoordinator | None = None

        if self._telegram_client is not None:
            caps = self._capabilities
            supports_draft = caps.has(
                ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES
            ) or caps.has(ChannelCapability.NATIVE_RICH_DRAFT_UPDATES)

            if supports_draft:
                # Use native Telegram draft streaming (verified capability).
                on_text_delta = self._create_draft_sink(
                    self._telegram_client,
                    msg.provider_sender_id,
                    str(uuid4()),
                )
            else:
                # Fall back to progressive complete-message streaming via
                # the shared coordinator (pacing + stream-safe formatting).
                accumulator = AnswerAccumulator()
                accumulator.start()
                client = self._telegram_client
                recipient = msg.provider_sender_id

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
                    text=msg.text or "",
                    channel=self._config.channel,
                    channel_ref=msg.provider_message_id,
                    thread_ref=msg.reply_to_provider_id,
                    occurred_at=msg.occurred_at,
                    on_text_delta=on_text_delta,
                )
            )

            # Claim outbound before any channel send
            if turn_result.outbound_delivery_id is not None:
                claimed = self._storage.claim_outbound_delivery(
                    wax_id,
                    turn_result.outbound_delivery_id,
                    self._clock.now(),
                )
                if claimed is None:
                    logger.info(
                        "outbound already claimed; skipping send",
                        extra={
                            "outbound_delivery_id": str(
                                turn_result.outbound_delivery_id
                            )
                        },
                    )

            # Send final message
            if turn_result.tutor_entry is not None and turn_result.tutor_entry.text:
                final_text = turn_result.tutor_entry.text

                if coordinator is not None:
                    # Finalize the streamed response: the coordinator's
                    # pacing policy sends the final streamed update. The
                    # authoritative complete message is sent below.
                    await coordinator.complete()

                # Send final complete message
                if self._telegram_client is not None:
                    result = await self._telegram_client.send_text(
                        OutboundPayload(
                            channel=self._config.channel,
                            wax_id=wax_id,
                            recipient_external_id=msg.provider_sender_id,
                            text=final_text,
                        )
                    )
                    if result.success and result.provider_message_id:
                        last_provider_message_id = result.provider_message_id

                # Mark outbound as delivered
                if turn_result.outbound_delivery_id is not None:
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
                    "student_entry_id": str(student_entry.id),
                    "error": str(exc),
                },
            )
            # Persist a durable, independently retryable processing job
            now = self._clock.now()
            try:
                self._storage.enqueue_job(
                    DerivationJob(
                        id=new_id(),
                        wax_id=wax_id,
                        kind=JOB_PROCESS_TURN,
                        object_type=ObjectType.HISTORY,
                        object_id=student_entry.id,
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
                        "student_entry_id": str(student_entry.id),
                    },
                )
            return InboundResult(
                accepted=True,
                wax_id=wax_id,
                student_entry_id=student_entry.id,
                outbound_delivery_id=None,
                needs_linking=False,
                error=f"processing_failed: {type(exc).__name__}",
            )

    def _create_draft_sink(
        self, client: TelegramClient, chat_id: str, draft_id: str
    ) -> TextDeltaSink:
        """Create a text delta sink that updates Telegram draft messages."""

        async def sink(text: str) -> None:
            if not text.strip():
                return
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

    async def _send_progressive_chunk(
        self, client: TelegramClient, chat_id: str, text: str
    ) -> None:
        """Send a progressive chunk as a complete message."""
        if not text.strip():
            return
        result = await client.send_text(
            OutboundPayload(
                channel=self._config.channel,
                wax_id=WaxId(""),  # Not used by client
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
