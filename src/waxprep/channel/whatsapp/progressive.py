"""Progressive WhatsApp delivery of a streaming tutor reply.

WhatsApp Cloud API does not support token-by-token streaming inside one
bubble. We send typing indicators and complete message chunks while the
model streams. Only the final full text is stored in history/outbox.

Chunk boundaries are infrastructure constants (size / paragraph), never
educational rules (no short-vs-long tutor branches).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

# Infrastructure defaults — not educational policy.
_CHUNK_SOFT_LIMIT = 4000
_PARAGRAPH_SEP = "\n\n"


class ProgressiveWhatsAppDelivery:
    """Accumulate stream deltas; emit complete WhatsApp texts progressively."""

    def __init__(
        self,
        *,
        send_text: Callable[[str], Awaitable[None]],
        send_typing: Callable[[], Awaitable[None]] | None = None,
        chunk_soft_limit: int = _CHUNK_SOFT_LIMIT,
    ) -> None:
        self._send_text = send_text
        self._send_typing = send_typing
        self._chunk_soft_limit = chunk_soft_limit
        self._buffer = ""
        self._emitted: list[str] = []

    @property
    def final_text(self) -> str:
        return "".join(self._emitted) + self._buffer

    async def on_delta(self, delta: str) -> None:
        if not delta:
            return
        if self._send_typing is not None and not self._buffer and not self._emitted:
            try:
                await self._send_typing()
            except Exception:
                logger.debug("typing indicator failed", exc_info=True)
        self._buffer += delta
        await self._flush_ready(final=False)

    async def finish(self) -> str:
        await self._flush_ready(final=True)
        return self.final_text

    async def _flush_ready(self, *, final: bool) -> None:
        while True:
            if final:
                piece = self._buffer.strip()
                self._buffer = ""
                if piece:
                    await self._send_text(piece)
                    self._emitted.append(piece)
                return
            # Prefer paragraph boundaries, else soft character limit.
            split_at = -1
            if _PARAGRAPH_SEP in self._buffer and len(self._buffer) >= 40:
                idx = self._buffer.rfind(_PARAGRAPH_SEP)
                if idx >= 20:
                    split_at = idx
            if split_at < 0 and len(self._buffer) >= self._chunk_soft_limit:
                # Break on last space before limit
                window = self._buffer[: self._chunk_soft_limit]
                sp = window.rfind(" ")
                split_at = sp if sp > 40 else self._chunk_soft_limit
            if split_at < 0:
                return
            piece = self._buffer[:split_at].strip()
            self._buffer = self._buffer[split_at:].lstrip()
            if piece:
                await self._send_text(piece)
                self._emitted.append(piece)


async def consume_text_stream(
    deltas: AsyncIterator[str],
    delivery: ProgressiveWhatsAppDelivery,
) -> str:
    """Feed text deltas into progressive delivery; return final full text."""
    async for delta in deltas:
        await delivery.on_delta(delta)
    return await delivery.finish()
