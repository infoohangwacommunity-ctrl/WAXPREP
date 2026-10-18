"""Outbox delivery service for WhatsApp messages.

This module implements the durable outbound delivery pattern:
1. Delivery records are created before any API calls
2. Failed deliveries are retried with exponential backoff
3. Delivery status callbacks are processed
4. The same stored reply is sent on retry (no re-teaching)

The outbox ensures:
- Every tutor reply is delivered at least once
- Temporary failures are retried safely
- Permanent failures are logged and marked
- Delivery status is tracked accurately
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from waxprep.channel.contracts import OutboundPayload
from waxprep.channel.whatsapp.client import WhatsAppClient
from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    HistoryEntry,
    OutboundDelivery,
    OutboundStatus,
    new_id,
)
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


def split_text_for_delivery(text: str, soft_limit: int | None = None) -> list[str]:
    """Split long tutor text into complete WhatsApp messages.

    Prefer one bubble; only split at platform max with paragraph/sentence cuts.
    soft_limit is ignored (kept for call-site compatibility).
    """
    from waxprep.channel.delivery import split_for_platform

    return split_for_platform(text, "whatsapp")


@dataclass(frozen=True, slots=True)
class OutboxConfig:
    """Configuration for outbox delivery."""

    channel: str = "whatsapp"
    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 300.0
    backoff_multiplier: float = 2.0
    batch_size: int = 10
    poll_interval_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """Result of a single delivery attempt."""

    success: bool
    provider_message_id: str | None
    temporary_failure: bool
    error_code: str | None
    error_detail: str | None
    retry_after: datetime | None


class WhatsAppOutbox:
    """Durable outbound delivery service for WhatsApp.

    The outbox implements the following guarantees:
    - Every delivery record is processed exactly once per attempt
    - Failed deliveries are retried with exponential backoff
    - The same stored reply text is sent on every retry
    - Delivery status is updated atomically with the attempt
    - Temporary failures (timeouts, 5xx) are retried
    - Permanent failures (4xx except 429) are marked as failed

    The outbox does NOT:
    - Modify the tutor reply text
    - Invoke the Teacher on retry
    - Delete delivery records (they are kept for audit)
    """

    def __init__(
        self,
        storage: Storage,
        client: WhatsAppClient,
        clock: Clock,
        *,
        config: OutboxConfig | None = None,
    ) -> None:
        self._storage = storage
        self._client = client
        self._clock = clock
        self._config = config or OutboxConfig()
        self._running = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the outbox delivery loop."""
        async with self._lock:
            if self._running:
                return
            self._running = True
        logger.info("whatsapp outbox started")
        while self._running:
            try:
                await self._process_due_deliveries()
                await asyncio.sleep(self._config.poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("outbox loop error")
                await asyncio.sleep(self._config.poll_interval_seconds)

    async def stop(self) -> None:
        """Stop the outbox delivery loop."""
        async with self._lock:
            self._running = False
        logger.info("whatsapp outbox stopped")

    async def _process_due_deliveries(self) -> None:
        """Process all due delivery records."""
        now = self._clock.now()
        due_deliveries = self._storage.list_due_outbound_deliveries(
            wax_id=None,  # Process all students
            now=now,
            limit=self._config.batch_size,
        )
        logger.debug(
            "processing due deliveries",
            extra={"count": len(due_deliveries)},
        )
        for delivery in due_deliveries:
            await self._process_delivery(delivery, now)

    async def _process_delivery(
        self, delivery: OutboundDelivery, now: datetime
    ) -> None:
        """Process a single delivery record."""
        logger.info(
            "processing delivery",
            extra={
                "delivery_id": str(delivery.id),
                "wax_id": str(delivery.wax_id),
                "attempts": delivery.attempts,
                "status": delivery.status,
            },
        )

        # Get the tutor entry to send
        tutor_entry = self._storage.get_history_entry(
            delivery.wax_id, delivery.tutor_entry_id
        )
        if tutor_entry is None:
            logger.error(
                "tutor entry not found for delivery",
                extra={"delivery_id": str(delivery.id)},
            )
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error="tutor_entry_not_found",
                retry_at=None,
            )
            return

        # Resolve recipient: channel identity for this student on WhatsApp
        channel_identity = self._resolve_channel_identity(delivery.wax_id)
        if channel_identity is None:
            logger.error(
                "channel identity not found for delivery",
                extra={"delivery_id": str(delivery.id)},
            )
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error="channel_identity_not_found",
                retry_at=None,
            )
            return

        # Build the outbound payload
        payload = OutboundPayload(
            channel=self._config.channel,
            wax_id=delivery.wax_id,
            recipient_external_id=channel_identity.external_id,
            text=tutor_entry.text,
            artifact_storage_refs=(),  # TODO: Add artifact support
            tutor_entry_id=delivery.tutor_entry_id,
        )

        # Attempt delivery
        attempt = await self._attempt_delivery(payload, delivery.attempts)

        # Update delivery status based on attempt result
        if attempt.success:
            self._storage.mark_outbound_delivered(
                delivery.wax_id,
                delivery.id,
                now,
                provider_message_id=attempt.provider_message_id,
            )
            logger.info(
                "delivery succeeded",
                extra={
                    "delivery_id": str(delivery.id),
                    "provider_message_id": attempt.provider_message_id,
                },
            )
        elif (
            attempt.temporary_failure and delivery.attempts < self._config.max_attempts
        ):
            next_attempt = delivery.attempts + 1
            delay = self._calculate_backoff(next_attempt)
            retry_at = now + timedelta(seconds=delay)
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error=attempt.error_detail or attempt.error_code or "temporary_failure",
                retry_at=retry_at,
            )
            logger.info(
                "delivery temporary failure, will retry",
                extra={
                    "delivery_id": str(delivery.id),
                    "attempt": next_attempt,
                    "retry_at": retry_at.isoformat(),
                },
            )
        else:
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error=attempt.error_detail or attempt.error_code or "permanent_failure",
                retry_at=None,
            )
            logger.warning(
                "delivery permanent failure",
                extra={
                    "delivery_id": str(delivery.id),
                    "attempts": delivery.attempts,
                    "error": attempt.error_detail,
                },
            )

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff delay."""
        delay = self._config.base_delay_seconds * (
            self._config.backoff_multiplier ** (attempt - 1)
        )
        return min(delay, self._config.max_delay_seconds)

    async def _attempt_delivery(
        self, payload: OutboundPayload, attempt: int
    ) -> DeliveryAttempt:
        """Attempt to deliver a message via WhatsApp API.

        Long replies are sent as successive complete messages (progressive
        delivery). Only the last provider message id is retained for status
        callbacks. History already holds the full final text.
        """
        try:
            typing = getattr(self._client, "send_typing", None)
            if callable(typing):
                try:
                    maybe = typing(payload.recipient_external_id)
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    logger.debug("typing indicator failed", exc_info=True)

            chunks = split_text_for_delivery(payload.text or "")
            if not chunks:
                return DeliveryAttempt(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=False,
                    error_code="empty_text",
                    error_detail="tutor reply text is empty",
                    retry_after=None,
                )

            last = None
            for chunk in chunks:
                piece = replace(payload, text=chunk)
                last = await self._client.send_text(piece)
                if not last.success:
                    return DeliveryAttempt(
                        success=False,
                        provider_message_id=last.provider_message_id,
                        temporary_failure=last.temporary_failure,
                        error_code=last.error_code,
                        error_detail=last.error_detail,
                        retry_after=None,
                    )
            assert last is not None
            return DeliveryAttempt(
                success=True,
                provider_message_id=last.provider_message_id,
                temporary_failure=False,
                error_code=None,
                error_detail=None,
                retry_after=None,
            )
        except Exception as exc:
            logger.exception(
                "delivery exception",
                extra={"attempt": attempt, "error": str(exc)},
            )
            return DeliveryAttempt(
                success=False,
                provider_message_id=None,
                temporary_failure=True,
                error_code=type(exc).__name__,
                error_detail=str(exc),
                retry_after=None,
            )

    async def handle_delivery_status(
        self,
        wax_id: WaxId,
        provider_message_id: str,
        status: str,
        timestamp: datetime | None = None,
    ) -> bool:
        """Process a WhatsApp delivery status callback.

        Returns True if the status was processed successfully.
        """
        now = self._clock.now()
        delivery = self._find_delivery_by_provider_id(wax_id, provider_message_id)
        if delivery is None:
            logger.warning(
                "delivery status for unknown message",
                extra={"provider_message_id": provider_message_id},
            )
            return False

        # Map WhatsApp status to our OutboundStatus
        if status in ("delivered", "read"):
            self._storage.mark_outbound_delivered(delivery.wax_id, delivery.id, now)
            logger.info(
                "delivery status updated",
                extra={
                    "delivery_id": str(delivery.id),
                    "provider_message_id": provider_message_id,
                    "status": status,
                },
            )
            return True
        elif status == "failed":
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error="provider_status_failed",
                retry_at=None,
            )
            logger.warning(
                "delivery failed per provider",
                extra={"delivery_id": str(delivery.id)},
            )
            return True
        else:
            logger.debug(
                "unknown delivery status",
                extra={"status": status, "provider_message_id": provider_message_id},
            )
            return False

    def _find_delivery_by_provider_id(
        self, wax_id: WaxId, provider_message_id: str
    ) -> OutboundDelivery | None:
        list_fn = getattr(self._storage, "list_outbound_deliveries_for_wax", None)
        if callable(list_fn):
            for d in list(list_fn(wax_id)):  # type: ignore[arg-type]
                if d.channel_ref == provider_message_id:
                    return d
        return None

    def _resolve_channel_identity(self, wax_id: WaxId):
        list_fn = getattr(self._storage, "list_channel_identities_for_wax", None)
        if callable(list_fn):
            for identity in list(list_fn(wax_id)):  # type: ignore[arg-type]
                if identity.channel == self._config.channel:
                    return identity
        return None

    async def enqueue_delivery(
        self,
        wax_id: WaxId,
        tutor_entry: HistoryEntry,
        channel_ref: str | None = None,
    ) -> OutboundDelivery:
        """Enqueue a new outbound delivery.

        This is called when a tutor reply is first saved and needs to be delivered.
        """
        now = self._clock.now()
        delivery = OutboundDelivery(
            id=new_id(),
            wax_id=wax_id,
            tutor_entry_id=tutor_entry.id,
            channel=self._config.channel,
            channel_ref=channel_ref,
            provider_message_id=None,
            status=OutboundStatus.PENDING,
            attempts=0,
            last_error=None,
            delivered_at=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
        created = self._storage.create_outbound_delivery(delivery)
        logger.info(
            "delivery enqueued",
            extra={
                "delivery_id": str(created.id),
                "tutor_entry_id": str(tutor_entry.id),
            },
        )
        return created
