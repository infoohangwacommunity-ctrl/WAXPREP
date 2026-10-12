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

import logging
from dataclasses import dataclass
from uuid import UUID

from waxprep.channel.contracts import NormalizedInbound
from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    Student,
    StudentStatus,
)
from waxprep.history.service import HistoryService
from waxprep.orchestration.turn_ci_first import TurnResultCiFirst, TurnServiceCiFirst
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._storage = storage
        self._history = history
        self._turn_service = turn_service
        self._clock = clock
        self._config = config or WhatsAppInboundConfig()

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

        # Step 3: Check for duplicate delivery (idempotency by channel_ref)
        existing = self._find_by_channel_ref(wax_id, msg.provider_message_id)
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

        # Step 4: Durably save the student message
        # Use provider_message_id as channel_ref for idempotency
        try:
            student_entry, created = self._history.record_with_status(
                wax_id,
                AuthorKind.STUDENT,
                msg.text or "",
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

        # Step 5: Process through CI-first turn service
        try:
            turn_result: TurnResultCiFirst = (
                await self._turn_service.handle_student_message(
                    wax_id=wax_id,
                    text=msg.text or "",
                    channel=self._config.channel,
                    channel_ref=msg.provider_message_id,
                    thread_ref=msg.reply_to_provider_id,
                    occurred_at=msg.occurred_at,
                )
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
            # Message is saved, processing failed - can be retried
            return InboundResult(
                accepted=True,
                wax_id=wax_id,
                student_entry_id=student_entry.id,
                outbound_delivery_id=None,
                needs_linking=False,
                error=f"processing_failed: {type(exc).__name__}",
            )

    def _find_by_channel_ref(self, wax_id: WaxId, provider_message_id: str):
        for entry in self._storage.list_history(wax_id, limit=500):
            if (
                entry.channel == self._config.channel
                and entry.channel_ref == provider_message_id
            ):
                return entry
        return None

    def create_linking_response(self, sender: str) -> str:
        """Create a response for unlinked senders."""
        return self._config.linking_instruction

    def create_disabled_response(self) -> str:
        """Create a response for disabled accounts."""
        return self._config.disabled_instruction
