"""Telegram durable outbound delivery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from waxprep.channel.contracts import OutboundPayload
from waxprep.channel.delivery import split_for_platform
from waxprep.channel.telegram.client import TelegramClient
from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import OutboundDelivery, OutboundStatus
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramOutboxConfig:
    channel: str = "telegram"
    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 300.0
    backoff_multiplier: float = 2.0
    batch_size: int = 10
    poll_interval_seconds: float = 1.0


class TelegramOutbox:
    def __init__(
        self,
        storage: Storage,
        client: TelegramClient,
        clock: Clock,
        config: TelegramOutboxConfig | None = None,
    ) -> None:
        self._storage = storage
        self._client = client
        self._clock = clock
        self._config = config or TelegramOutboxConfig()
        self._running = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
        logger.info("telegram outbox started")
        while self._running:
            try:
                await self._process_due()
                await asyncio.sleep(self._config.poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("telegram outbox loop error")
                await asyncio.sleep(self._config.poll_interval_seconds)

    async def stop(self) -> None:
        async with self._lock:
            self._running = False

    async def _process_due(self) -> None:
        now = self._clock.now()
        due = self._storage.list_due_outbound_deliveries(
            wax_id=None, now=now, limit=self._config.batch_size
        )
        for delivery in due:
            if delivery.channel != self._config.channel:
                continue
            await self._deliver(delivery)

    def _resolve_identity(self, wax_id: WaxId):
        list_fn = getattr(self._storage, "list_channel_identities_for_wax", None)
        if callable(list_fn):
            identities = list_fn(wax_id)
            for identity in list(identities):  # type: ignore[arg-type]
                if identity.channel == self._config.channel:
                    return identity
        return None

    async def _deliver(self, delivery: OutboundDelivery) -> None:
        """Deliver one record with CAS claim, backoff, and at-least-once.

        Mirrors the WhatsApp outbox contract: exactly one worker may claim
        a pending record; temporary failures (timeouts, 429, 5xx) are
        retried with exponential backoff up to max_attempts; permanent
        failures are marked failed. The same stored reply text is sent on
        every retry (no re-teaching).
        """
        now = self._clock.now()
        if delivery.status is OutboundStatus.DELIVERED:
            return
        if delivery.status is OutboundStatus.FAILED:
            return

        # Cross-replica CAS: only one outbox worker may send this delivery.
        claimed = self._storage.claim_outbound_delivery(
            delivery.wax_id, delivery.id, now
        )
        if claimed is None:
            logger.info(
                "outbound claim lost (already claimed or delivered)",
                extra={"delivery_id": str(delivery.id)},
            )
            return
        delivery = claimed

        identity = self._resolve_identity(delivery.wax_id)
        if identity is None:
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error="channel_identity_not_found",
                retry_at=None,
            )
            return
        entry = self._storage.get_history_entry(
            delivery.wax_id, delivery.tutor_entry_id
        )
        text = (entry.text if entry is not None else None) or ""
        if not text.strip():
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error="empty_tutor_text",
                retry_at=None,
            )
            return

        last_mid = None
        try:
            for chunk in split_for_platform(text, self._config.channel):
                result = await self._client.send_text(
                    OutboundPayload(
                        channel=self._config.channel,
                        wax_id=delivery.wax_id,
                        recipient_external_id=identity.external_id,
                        text=chunk,
                        tutor_entry_id=delivery.tutor_entry_id,
                    )
                )
                if not result.success:
                    self._record_failure(
                        delivery,
                        result.temporary_failure,
                        result.error_detail or result.error_code or "send_failed",
                    )
                    return
                last_mid = result.provider_message_id
        except Exception as exc:
            self._record_failure(
                delivery, True, f"send_exception: {type(exc).__name__}"
            )
            return

        self._storage.mark_outbound_delivered(
            delivery.wax_id,
            delivery.id,
            self._clock.now(),
            provider_message_id=last_mid,
        )

    def _record_failure(
        self,
        delivery: OutboundDelivery,
        temporary: bool,
        error: str,
    ) -> None:
        """Mark a failed attempt: retryable backoff or permanent failure."""
        now = self._clock.now()
        if temporary and delivery.attempts < self._config.max_attempts:
            next_attempt = delivery.attempts + 1
            delay = self._config.base_delay_seconds * (
                self._config.backoff_multiplier ** (next_attempt - 1)
            )
            delay = min(delay, self._config.max_delay_seconds)
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error=error,
                retry_at=now + timedelta(seconds=delay),
            )
            logger.info(
                "telegram delivery temporary failure, will retry",
                extra={
                    "delivery_id": str(delivery.id),
                    "attempt": next_attempt,
                },
            )
        else:
            self._storage.mark_outbound_failed(
                delivery.wax_id,
                delivery.id,
                now,
                error=error,
                retry_at=None,
            )
            logger.warning(
                "telegram delivery permanent failure",
                extra={
                    "delivery_id": str(delivery.id),
                    "attempts": delivery.attempts,
                    "error": error,
                },
            )


__all__ = ["TelegramOutbox", "TelegramOutboxConfig"]
