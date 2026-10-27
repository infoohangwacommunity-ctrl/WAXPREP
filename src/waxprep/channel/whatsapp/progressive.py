"""Progressive WhatsApp delivery of a streaming tutor reply.

Blueprint §6: WhatsApp does not support a native live-draft API. The
default path produces ONE coherent final message. Progressive
complete-message delivery is an explicit, opt-in strategy — not the
default, and not a paragraph-per-message splitter.

The authoritative final answer is reconstructed from the ordered deltas
by the ``AnswerAccumulator`` — NOT by concatenating trimmed outbound
messages (blueprint §6.3). The previous implementation lost paragraph
boundaries because it stored stripped emitted pieces and joined them.

Chunk boundaries are infrastructure constants (size), never educational
rules (no short-vs-long tutor branches).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from waxprep.channel.streaming.accumulator import AnswerAccumulator

logger = logging.getLogger(__name__)


class ProgressiveWhatsAppDelivery:
    """Accumulate stream deltas; send ONE coherent final message by default.

    The default behaviour (blueprint §6.1):
    1. Start generation.
    2. Accumulate the answer (no premature sends).
    3. Send one coherent final message when the answer completes.

    Progressive complete-message delivery is an explicit opt-in via
    ``progressive=True``. When enabled, the delivery sends complete
    message units only at safe boundaries — never arbitrary fragments.
    """

    def __init__(
        self,
        *,
        send_text: Callable[[str], Awaitable[None]],
        send_typing: Callable[[], Awaitable[None]] | None = None,
        chunk_soft_limit: int = 4000,
        progressive: bool = False,
    ) -> None:
        self._send_text = send_text
        self._send_typing = send_typing
        self._chunk_soft_limit = chunk_soft_limit
        self._progressive = progressive
        self._accumulator = AnswerAccumulator()
        self._accumulator.start()
        self._sent_progressive: list[str] = []

    @property
    def final_text(self) -> str:
        """The authoritative final answer — reconstructed from ordered
        deltas, never from trimmed outbound messages."""
        return self._accumulator.final_text

    @property
    def response_id(self):
        return self._accumulator.response_id

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

    async def finish(self) -> str:
        """Finalize: return the authoritative final text.

        If progressive delivery was used, the already-sent chunks are
        tracked. The caller (outbox) decides whether to send a final
        complete message or rely on the progressive chunks. The
        authoritative answer is always ``self.final_text``.
        """
        self._accumulator.complete()
        return self.final_text

    async def flush_final(self, text: str | None = None) -> str:
        """Send any complete message units not yet delivered.

        After progressive delivery the last (still-growing) unit was
        never sent progressively, and a mid-stream fallback can make
        the authoritative answer differ from what was already sent.
        This sends every complete unit of ``text`` (default: the
        accumulated final text) that was not already delivered, so the
        student always ends with the whole answer. No-op when nothing
        was sent progressively — the caller sends the final message
        itself.
        """
        final = (text if text is not None else self.final_text) or ""
        if not self._sent_progressive or not final.strip():
            return final
        from waxprep.channel.delivery import split_for_platform

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
        """If progressive mode is enabled, check whether enough safe text
        has accumulated to send a complete message unit. Does NOT split
        on every paragraph — only when the soft limit is reached."""
        text = self._accumulator.final_text
        if len(text) < self._chunk_soft_limit:
            return
        from waxprep.channel.delivery import split_for_platform

        chunks = split_for_platform(text, "whatsapp")
        if len(chunks) <= 1:
            return
        # Send all but the last chunk (the last chunk is still growing).
        for chunk in chunks[:-1]:
            if chunk and chunk not in self._sent_progressive:
                await self._send_text(chunk)
                self._sent_progressive.append(chunk)


async def consume_text_stream(
    deltas: AsyncIterator[str],
    delivery: ProgressiveWhatsAppDelivery,
) -> str:
    """Feed text deltas into progressive delivery; return final full text."""
    async for delta in deltas:
        await delivery.on_delta(delta)
    return await delivery.finish()
