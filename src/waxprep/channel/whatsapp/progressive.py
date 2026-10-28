"""Progressive WhatsApp delivery of a streaming tutor reply.

WhatsApp has no native live-draft API. Two delivery strategies exist:

1. Default (``progressive=False``): accumulate silently, send ONE
   coherent final message. Blueprint §6.1.
2. Progressive complete-message delivery (``progressive=True``): send
   complete message units while the Teacher generates.
   - With ``early_flush_chars`` unset, units flush only when the text
     exceeds the platform soft limit (legacy behaviour, blueprint §6.2).
   - With ``early_flush_chars`` set (opt-in), units also flush EARLY at
     safe paragraph/sentence boundaries once enough new text exists, so
     the student starts seeing the answer within seconds instead of
     waiting for the whole generation.

The authoritative final answer is reconstructed from the ordered deltas
by the ``AnswerAccumulator`` — NOT by concatenating trimmed outbound
messages (blueprint §6.3).

Chunk boundaries are infrastructure constants (size), never educational
rules (no short-vs-long tutor branches).

Send failures never raise into the model stream: a failed chunk is
counted and the remaining text stays recoverable via the durable outbox
(``had_failures`` tells the caller to schedule a retry).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from waxprep.channel.streaming.accumulator import AnswerAccumulator
from waxprep.channel.streaming.formatter import safe_increment

logger = logging.getLogger(__name__)


def _latest_display_boundary(text: str) -> int:
    """Index just past the latest safe paragraph/sentence boundary.

    Prefers a blank-line (paragraph) boundary; falls back to a sentence
    end followed by whitespace. Returns 0 when neither exists yet.
    """
    cut = text.rfind("\n\n")
    if cut != -1:
        return cut + 2
    for end in (". ", "! ", "? "):
        idx = text.rfind(end)
        if idx != -1:
            return idx + len(end)
    for end in (".\n", "!\n", "?\n", ".\n\n"):
        idx = text.rfind(end)
        if idx != -1:
            return idx + len(end)
    return 0


class ProgressiveWhatsAppDelivery:
    """Accumulate stream deltas; deliver complete WhatsApp messages."""

    def __init__(
        self,
        *,
        send_text: Callable[[str], Awaitable[None]],
        send_typing: Callable[[], Awaitable[None]] | None = None,
        chunk_soft_limit: int = 4000,
        progressive: bool = False,
        early_flush_chars: int | None = None,
        steady_flush_chars: int | None = None,
    ) -> None:
        self._send_text = send_text
        self._send_typing = send_typing
        self._chunk_soft_limit = chunk_soft_limit
        self._progressive = progressive
        self._early_flush_chars = early_flush_chars
        # Escalating pacing: the FIRST unit flushes as soon as
        # ``early_flush_chars`` display-safe characters exist (fast first
        # content), later units wait for ``steady_flush_chars`` so bubbles
        # stay substantial instead of fragmenting the answer into many
        # tiny messages. Defaults to ``early_flush_chars`` when unset.
        self._steady_flush_chars = steady_flush_chars
        self._accumulator = AnswerAccumulator()
        self._accumulator.start()
        self._sent_progressive: list[str] = []
        # Index into the accumulated text up to which text has been sent
        # progressively (early-flush mode only).
        self._sent_upto = 0
        self._send_failures = 0

    @property
    def final_text(self) -> str:
        """The authoritative final answer — reconstructed from ordered
        deltas, never from trimmed outbound messages."""
        return self._accumulator.final_text

    @property
    def response_id(self):
        return self._accumulator.response_id

    @property
    def had_failures(self) -> bool:
        """Whether any progressive send failed. The caller should leave
        the outbound record retryable so the durable outbox recovers."""
        return self._send_failures > 0

    async def on_delta(self, delta: str) -> None:
        if not delta:
            return
        if self._send_typing is not None and self._accumulator.delta_count == 0:
            try:
                await self._send_typing()
            except Exception:
                logger.debug("typing indicator failed", exc_info=True)
        self._accumulator.append_delta(delta)
        if self._progressive:
            await self._maybe_send_progressive()

    async def _send_unit(self, unit: str) -> bool:
        """Send one complete message unit; return success (never raise)."""
        from waxprep.channel.delivery import split_for_platform

        for piece in split_for_platform(unit, "whatsapp"):
            if not piece or piece in self._sent_progressive:
                continue
            try:
                await self._send_text(piece)
                self._sent_progressive.append(piece)
            except Exception:
                self._send_failures += 1
                logger.warning(
                    "progressive chunk send failed; outbox will recover",
                    exc_info=True,
                )
                return False
        return True

    async def finish(self) -> str:
        """Finalize: return the authoritative final text.

        If progressive delivery was used, the already-sent chunks are
        tracked. The caller (handler/outbox) decides whether to send a
        final complete message or rely on the progressive chunks. The
        authoritative answer is always ``self.final_text``.
        """
        self._accumulator.complete()
        return self.final_text

    async def flush_final(self, text: str | None = None) -> str:
        """Send every complete message unit not yet delivered.

        After progressive delivery the last (still-growing) unit was
        never sent progressively, and a mid-stream fallback can make the
        authoritative answer differ from what was already sent. This
        sends every remaining unit of ``text`` (default: the accumulated
        final text) so the student always ends with the whole answer.
        No-op when nothing was sent progressively — the caller sends the
        final message itself.

        Raises the first send failure so the caller can leave the
        outbound record retryable instead of marking it delivered.
        """
        final = (text if text is not None else self.final_text) or ""
        # Proceed when units were sent progressively OR a progressive
        # attempt failed (a failed first attempt leaves nothing in
        # ``_sent_progressive`` — the whole answer must still be sent).
        if (
            not self._sent_progressive and self._send_failures == 0
        ) or not final.strip():
            return final
        from waxprep.channel.delivery import split_for_platform

        if self._sent_upto > 0:
            # Early-flush mode: send everything after the last flushed
            # boundary (the never-sent growing tail, or the whole
            # authoritative answer when streaming fell back mid-way).
            remaining = final[self._sent_upto :]
            for piece in split_for_platform(remaining, "whatsapp"):
                if not piece.strip():
                    continue
                await self._send_text(piece)
                self._sent_progressive.append(piece)
            return final

        for chunk in split_for_platform(final, "whatsapp"):
            if chunk and chunk not in self._sent_progressive:
                await self._send_text(chunk)
                self._sent_progressive.append(chunk)
        return final

    @property
    def progressive_chunks_sent(self) -> int:
        """How many progressive message units were sent. Used by the
        outbox for duplicate-aware finalization (blueprint §6.7)."""
        return len(self._sent_progressive)

    async def _maybe_send_progressive(self) -> None:
        """Send complete message units when enough safe text exists."""
        text = self._accumulator.final_text

        if self._early_flush_chars is not None:
            steady = self._steady_flush_chars or self._early_flush_chars
            min_chars = self._early_flush_chars if self._sent_upto == 0 else steady
            await self._maybe_send_early(text, min_chars)
            return

        # Legacy behaviour: flush only past the soft limit.
        if len(text) < self._chunk_soft_limit:
            return
        from waxprep.channel.delivery import split_for_platform

        chunks = split_for_platform(text, "whatsapp")
        if len(chunks) <= 1:
            return
        # Send all but the last chunk (the last chunk is still growing).
        for chunk in chunks[:-1]:
            if chunk and chunk not in self._sent_progressive:
                await self._send_unit(chunk)

    async def _maybe_send_early(self, text: str, min_chars: int) -> None:
        """Early-flush: send complete units at safe boundaries as the
        stream grows, so the first answer text reaches the student within
        seconds. Units are whole paragraphs/sentences — never fragments."""
        if self._send_failures:
            # A progressive send already failed mid-stream. Stop early
            # flushing (no retry storm inside the model stream); flush_final
            # re-delivers every unit from the last acknowledged boundary.
            return
        inc = safe_increment(text)
        safe_text = inc.safe_text
        if not safe_text.strip():
            return
        boundary = _latest_display_boundary(safe_text)
        if boundary <= self._sent_upto:
            return
        new_chars = boundary - self._sent_upto
        if new_chars < min_chars:
            return
        unit = text[self._sent_upto : boundary]
        if not unit.strip():
            return
        sent_ok = await self._send_unit(unit)
        if sent_ok:
            # Advance ONLY on success: a failed unit must stay un-sent so
            # flush_final re-delivers it (never silently dropped).
            self._sent_upto = boundary


async def consume_text_stream(
    deltas: AsyncIterator[str],
    delivery: ProgressiveWhatsAppDelivery,
) -> str:
    """Feed text deltas into progressive delivery; return final full text."""
    async for delta in deltas:
        await delivery.on_delta(delta)
    return await delivery.finish()
