"""Streaming coordinator — owns the response lifecycle, selects a delivery
strategy from declared channel capabilities.

Blueprint §3.2 (Streaming coordinator):
- Owns the response lifecycle.
- Receives answer updates from the accumulator.
- Consults the channel's advertised capabilities.
- Applies shared pacing and cancellation rules.
- Chooses the appropriate delivery strategy without asking the Teacher
  to branch on the channel.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from waxprep.channel.streaming.accumulator import AnswerAccumulator
from waxprep.channel.streaming.contract import (
    ChannelCapabilities,
    ChannelCapability,
    DeliveryStrategy,
    ResponseLifecycle,
)
from waxprep.channel.streaming.formatter import safe_increment
from waxprep.channel.streaming.pacing import PacingDecision, PacingPolicy

logger = logging.getLogger(__name__)


# Type alias for the channel adapter's "send update" callback.
# The callback receives the safe-to-display text and returns the
# provider's acknowledgement (e.g. a message ID or draft ID).
SendUpdateFn = Callable[[str], Awaitable[str | None]]


@dataclass
class StreamingCoordinator:
    """Coordinates one streaming response's delivery.

    The coordinator:
    1. Receives answer deltas from the accumulator.
    2. Runs them through the stream-safe formatter.
    3. Checks the pacing policy.
    4. If the pacing decision is "send", calls the channel adapter's
       send-update callback with the safe text.
    5. On completion, calls the finalize callback.

    The coordinator never decides what the answer says. It only decides
    WHEN to send an update and WHAT SAFE TEXT to include.

    Lifecycle (blueprint §9.4):
    - A cancelled response stops accepting deltas.
    - A completed response does not send further updates.
    - An old streaming task cannot overwrite a newer response.
    """

    accumulator: AnswerAccumulator
    capabilities: ChannelCapabilities
    pacing: PacingPolicy
    send_update: SendUpdateFn
    finalize: Callable[[str], Awaitable[str | None]] | None = None
    _last_update_text: str = ""
    _last_update_at: datetime | None = None
    _last_sent_text: str = ""
    _provider_message_ids: list[str] = field(default_factory=list)

    @property
    def strategy(self) -> DeliveryStrategy:
        """Select a delivery strategy from the channel's capabilities."""
        caps = self.capabilities
        if caps.has(ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES):
            return DeliveryStrategy.NATIVE_DRAFT
        if caps.has(ChannelCapability.TYPING_INDICATOR):
            return DeliveryStrategy.TYPING_PLUS_FINAL
        return DeliveryStrategy.FINAL_ONLY

    async def on_delta(self, delta: str) -> None:
        """Process one answer delta. Appends to the accumulator, checks
        pacing, and sends an update if the pacing policy says to."""
        if self.accumulator.state is not ResponseLifecycle.ACTIVE:
            return  # cancelled or completed — ignore stale deltas
        self.accumulator.append_delta(delta)
        await self._maybe_send_update(is_final=False)

    async def complete(self) -> str:
        """Finalize the response. Returns the authoritative final text."""
        if self.accumulator.state is ResponseLifecycle.ACTIVE:
            await self._maybe_send_update(is_final=True)
            self.accumulator.complete()
        if self.finalize is not None:
            provider_id = await self.finalize(self.accumulator.final_text)
            if provider_id:
                self._provider_message_ids.append(provider_id)
        return self.accumulator.final_text

    async def cancel(self) -> None:
        """Cancel the response. Stops accepting deltas and prevents
        stale finalization (blueprint §9.4)."""
        self.accumulator.cancel()

    @property
    def provider_message_ids(self) -> tuple[str, ...]:
        """The provider message IDs for every sent update + the final
        message. Used by the outbox for reconciliation (blueprint §6.7)."""
        return tuple(self._provider_message_ids)

    async def _maybe_send_update(self, *, is_final: bool) -> None:
        """Check the pacing policy and send an update if warranted."""
        full_text = self.accumulator.final_text
        inc = safe_increment(full_text)
        safe_text = inc.safe_text

        now = datetime.now(UTC)
        elapsed = (
            (now - self._last_update_at).total_seconds()
            if self._last_update_at
            else float("inf")
        )
        new_chars = len(safe_text) - len(self._last_sent_text)
        if new_chars < 0:
            new_chars = 0

        has_boundary = bool(inc.pending_suffix)  # there's a clean cut

        decision: PacingDecision = self.pacing.should_send_update(
            new_chars=new_chars,
            elapsed_since_last=elapsed,
            has_safe_boundary=has_boundary,
            is_final=is_final,
        )

        if not decision.should_update:
            return

        # Don't send an empty update.
        if not safe_text:
            return

        # Don't send an identical update.
        if safe_text == self._last_sent_text and not is_final:
            return

        try:
            provider_id = await self.send_update(safe_text)
            self._last_update_text = safe_text
            self._last_update_at = now
            self._last_sent_text = safe_text
            if provider_id:
                self._provider_message_ids.append(provider_id)
        except Exception:
            logger.exception("streaming update failed; will retry on next delta")


__all__ = ["StreamingCoordinator"]
