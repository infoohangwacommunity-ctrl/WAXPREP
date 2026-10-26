"""Durable inbound WhatsApp message handler.

This module provides the production-ready inbound processing pipeline:
1. Verify webhook signature
2. Normalize message payload
3. Resolve sender identity (ChannelIdentity -> WaxId)
4. Durably save the inbound message
5. Queue background processing (CI, Teacher, etc.)
6. Return success only after durable acceptance

The handler ensures:
- Every message is saved exactly once (idempotency via provider_message_id)
- Unknown senders receive a clear linking instruction
- Processing failures don't lose messages
- No direct model calls in the webhook path
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from waxprep.channel.contracts import (
    CONTENT_KIND_INTERACTIVE_BUTTON,
    CONTENT_KIND_INTERACTIVE_LIST,
    InteractiveSuggestion,
    NormalizedInbound,
    OutboundPayload,
)
from waxprep.channel.whatsapp.progressive import ProgressiveWhatsAppDelivery
from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    InteractiveOffer,
    InteractiveOfferStatus,
    JobStatus,
    ObjectType,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.index.indexer import JOB_INGEST_MEDIA, JOB_PROCESS_TURN
from waxprep.orchestration.turn_ci_first import TurnResultCiFirst, TurnServiceCiFirst
from waxprep.storage.protocol import Storage

if TYPE_CHECKING:
    from waxprep.channel.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)

# Job kind for retrying CI+Teacher processing after a transient failure.
# A worker can claim and run these jobs independently of the webhook path,
# so an accepted student message is never stranded.


@dataclass(frozen=True, slots=True)
class InboundResult:
    """Result of processing an inbound WhatsApp message."""

    accepted: bool
    wax_id: WaxId | None
    student_entry_id: UUID | None
    outbound_delivery_id: UUID | None
    needs_linking: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WhatsAppInboundConfig:
    """Configuration for WhatsApp inbound processing."""

    channel: str = "whatsapp"
    max_attempts: int = 3
    retry_base_delay: float = 1.0
    linking_instruction: str = (
        "Please link your WhatsApp number to your WAXPREP account first. "
        "Contact support to complete registration."
    )
    disabled_instruction: str = (
        "Your account is currently disabled. Please contact support."
    )


class WhatsAppInboundHandler:
    """Production inbound WhatsApp message handler.

    This handler implements the complete inbound pipeline:
    - Resolves sender identity via ChannelIdentity lookup
    - Durably saves the message before any processing
    - Connects to CI-first turn service for processing
    - Handles unknown, disabled, and malformed cases
    - Provides idempotency via (channel, provider_message_id)

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
        config: WhatsAppInboundConfig | None = None,
        whatsapp_client: WhatsAppClient | None = None,
    ) -> None:
        self._storage = storage
        self._history = history
        self._turn_service = turn_service
        self._clock = clock
        self._config = config or WhatsAppInboundConfig()
        # When set, live turns stream complete WhatsApp message chunks while
        # the Teacher generates (progressive delivery). Outbox remains the
        # durable recovery path for the final authoritative text.
        self._whatsapp_client = whatsapp_client
        self._message_locks: dict[str, asyncio.Lock] = {}
        self._message_locks_guard = asyncio.Lock()

    async def _lock_for_message(self, provider_message_id: str) -> asyncio.Lock:
        async with self._message_locks_guard:
            lock = self._message_locks.get(provider_message_id)
            if lock is None:
                lock = asyncio.Lock()
                self._message_locks[provider_message_id] = lock
            return lock

    def resolve_sender(self, sender: str) -> WaxId | None:
        """Resolve a WhatsApp phone number to a WAX ID.

        This is the production sender-resolution function that replaces
        the placeholder in app.py.
        """
        identity = self._storage.get_channel_identity(self._config.channel, sender)
        if identity is not None:
            return identity.wax_id
        return None

    def _ensure_student_exists(self, wax_id: WaxId) -> Student | None:
        """Check if student exists and is active."""
        student = self._storage.get_student(wax_id)
        if student is None:
            logger.warning(
                "resolved wax_id has no student record",
                extra={"wax_id": str(wax_id)},
            )
            return None
        if student.status == StudentStatus.DISABLED:
            logger.warning(
                "student account is disabled",
                extra={"wax_id": str(wax_id)},
            )
            return None
        return student

    async def handle_message(self, msg: NormalizedInbound) -> InboundResult:
        """Serialize per WhatsApp message id, then process once."""
        mid = (msg.provider_message_id or "").strip()
        if mid:
            lock = await self._lock_for_message(mid)
            async with lock:
                return await self._handle_message_unlocked(msg)
        return await self._handle_message_unlocked(msg)

    async def _handle_message_unlocked(self, msg: NormalizedInbound) -> InboundResult:
        """Process a normalized inbound WhatsApp message.

        This is the main entry point called from the webhook route.
        It implements the complete durable acceptance pattern.

        Returns:
            InboundResult with processing outcome
        """
        logger.info(
            "whatsapp inbound processing started",
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
                "unresolved whatsapp sender",
                extra={"sender": msg.provider_sender_id},
            )
            return InboundResult(
                accepted=True,  # We accepted the webhook
                wax_id=None,
                student_entry_id=None,
                outbound_delivery_id=None,
                needs_linking=True,
                error=None,
            )

        # Step 2: Check student status
        student = self._ensure_student_exists(wax_id)
        if student is None:
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

        # Step 3: Check for duplicate delivery (idempotency by channel_ref).
        # Use the indexed unique lookup instead of scanning 500 entries.
        existing = self._storage.find_history_by_channel_ref(
            wax_id, self._config.channel, msg.provider_message_id
        )
        if existing is not None:
            logger.info(
                "duplicate whatsapp message detected",
                extra={
                    "provider_message_id": msg.provider_message_id,
                    "wax_id": str(wax_id),
                },
            )
            # Find existing tutor reply
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
            # Previous attempt crashed before tutor reply was saved
            # We'll resume processing below

        # Step 3b: One-shot interactive button/list consume.
        # Meta does not disable old buttons; we reject stale clicks at channel layer.
        if msg.content_kind in (
            CONTENT_KIND_INTERACTIVE_BUTTON,
            CONTENT_KIND_INTERACTIVE_LIST,
        ):
            button_id = str(
                msg.structured.get("button_id") or msg.structured.get("row_id") or ""
            )
            if button_id:
                offer = self._storage.find_interactive_offer_by_button_id(
                    wax_id, button_id
                )
                if offer is None:
                    logger.info(
                        "stale or unknown interactive click rejected",
                        extra={
                            "wax_id": str(wax_id),
                            "button_id": button_id,
                        },
                    )
                    # Optional single polite line — do not run full teaching turn.
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
                    return InboundResult(
                        accepted=True,
                        wax_id=wax_id,
                        student_entry_id=None,
                        outbound_delivery_id=None,
                        needs_linking=False,
                        error="interactive_offer_consumed",
                    )

        # Step 4: Durably save the student message
        # Use provider_message_id as channel_ref for idempotency
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

            # Non-blocking media derivation: enqueue after durable save so the
            # first tutor reply is never gated on download/OCR/STT.
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
                # Duplicate detected at history level
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

        # Step 5: Process through CI-first turn service.
        # When a WhatsApp client is available, wire progressive delivery so
        # the student receives complete message chunks while the model streams.
        progressive: ProgressiveWhatsAppDelivery | None = None
        on_text_delta = None
        last_provider_message_id: str | None = None
        if self._whatsapp_client is not None:
            client = self._whatsapp_client
            recipient = msg.provider_sender_id

            async def _send_chunk(text: str) -> None:
                nonlocal last_provider_message_id
                result = await client.send_text(
                    OutboundPayload(
                        channel=self._config.channel,
                        wax_id=wax_id,
                        recipient_external_id=recipient,
                        text=text,
                    )
                )
                if result.success and result.provider_message_id:
                    last_provider_message_id = result.provider_message_id

            async def _send_typing() -> None:
                await client.send_typing(recipient)

            progressive = ProgressiveWhatsAppDelivery(
                send_text=_send_chunk,
                send_typing=_send_typing,
                progressive=True,  # Enable progressive delivery
            )
            on_text_delta = progressive.on_delta

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

            # Claim outbound before any channel send so the outbox worker
            # cannot deliver the same tutor text a second time.
            if turn_result.outbound_delivery_id is not None:
                claimed = self._storage.claim_outbound_delivery(
                    wax_id,
                    turn_result.outbound_delivery_id,
                    self._clock.now(),
                )
                if claimed is None:
                    # Already claimed/delivered by another path — skip send.
                    logger.info(
                        "outbound already claimed; skipping progressive send",
                        extra={
                            "outbound_delivery_id": str(
                                turn_result.outbound_delivery_id
                            )
                        },
                    )
                    progressive = None

            # Flush any remaining progressive buffer after the Teacher finishes.
            if progressive is not None:
                try:
                    await progressive.finish()
                except Exception:
                    logger.debug(
                        "progressive delivery finish failed",
                        exc_info=True,
                    )
                # The default strategy is FINAL_ONLY (blueprint §6.1): one
                # coherent final message. If no progressive chunks were sent,
                # send the complete final text now via the channel client.
                # If progressive chunks WERE sent, they are already delivered;
                # do not duplicate the full answer.
                final_text = progressive.final_text.strip()
                if final_text and progressive.progressive_chunks_sent == 0:
                    # FINAL_ONLY: send the complete answer as one message.
                    try:
                        result = await client.send_text(
                            OutboundPayload(
                                channel=self._config.channel,
                                wax_id=wax_id,
                                recipient_external_id=recipient,
                                text=progressive.final_text,
                            )
                        )
                        if result.success and result.provider_message_id:
                            last_provider_message_id = result.provider_message_id
                    except Exception:
                        logger.debug(
                            "final-only delivery failed",
                            exc_info=True,
                        )
                # Mark the outbound record delivered so the outbox does
                # not re-send the full text. The progressive delivery (or
                # the final-only send above) IS the delivery.
                if turn_result.outbound_delivery_id is not None and final_text:
                    try:
                        self._storage.mark_outbound_delivered(
                            wax_id,
                            turn_result.outbound_delivery_id,
                            self._clock.now(),
                            provider_message_id=last_provider_message_id,
                        )
                    except Exception:
                        logger.debug(
                            "mark progressive outbound delivered failed",
                            exc_info=True,
                        )

            # Optional interactive presentation (AI-chosen). Opaque ids only.
            if (
                turn_result.interactive is not None
                and self._whatsapp_client is not None
                and not turn_result.duplicate
            ):
                await self._issue_interactive(
                    wax_id=wax_id,
                    recipient=msg.provider_sender_id,
                    suggestion=turn_result.interactive,
                    tutor_entry_id=turn_result.tutor_entry.id
                    if turn_result.tutor_entry
                    else None,
                    outbound_delivery_id=turn_result.outbound_delivery_id,
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
                    "progressive": progressive is not None,
                    "interactive": turn_result.interactive is not None,
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
            # Message is saved, processing failed. Persist a durable,
            # independently retryable processing job so a worker can recover
            # this turn after a timeout, process restart, or deployment.
            # The message is accepted as saved but NOT as fully processed.
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

    async def _issue_interactive(
        self,
        *,
        wax_id: WaxId,
        recipient: str,
        suggestion: InteractiveSuggestion,
        tutor_entry_id: UUID | None,
        outbound_delivery_id: UUID | None,
    ) -> None:
        """Send optional reply buttons and register a one-shot offer.

        Opaque button ids only. On WhatsApp rejection (e.g. outside 24h
        window), fall back silently — body teaching was already delivered
        via progressive or will be delivered by the outbox.
        """
        client = self._whatsapp_client
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
        try:
            result = await client.send_interactive_buttons(
                recipient_external_id=recipient,
                body_text=body[:1024],
                buttons=issued,
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
            try:
                self._storage.mark_outbound_delivered(
                    wax_id,
                    outbound_delivery_id,
                    now,
                    provider_message_id=result.provider_message_id,
                )
            except Exception:
                logger.debug("mark outbound after interactive failed", exc_info=True)

    def create_linking_response(self, sender: str) -> str:
        """Create a response for unlinked senders."""
        return self._config.linking_instruction

    def create_disabled_response(self) -> str:
        """Create a response for disabled accounts."""
        return self._config.disabled_instruction
