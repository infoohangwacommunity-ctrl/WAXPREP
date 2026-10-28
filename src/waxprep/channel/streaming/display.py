"""Stream-safe display filtering for the interactive-choices trailer.

The Teacher may optionally end a reply with the ``<<<choices>>>`` marker
followed by up to three short next-step lines. The completed reply is
parsed by the Teacher itself (marker stripped before persistence), but a
streaming display path receives RAW model deltas — a marker that arrives
mid-stream must never leak to the student as visible text.

The filter is infrastructure-only: it never rewrites the answer, it only
decides which prefix of the raw stream is safe to display right now.

Boundary handling: the marker may be split across deltas (e.g. ``<<<ch``
+
``oices>>>``). The filter therefore holds back the longest tail of the
buffer that could still be the start of the marker, and releases it once
it provably is not (or flushes it at finalization).
"""

from __future__ import annotations

from waxprep.channel.contracts import INTERACTIVE_CHOICES_MARKER

__all__ = ["ChoicesMarkerFilter"]


class ChoicesMarkerFilter:
    """Feed raw deltas; get back only what is safe to display now."""

    def __init__(self, marker: str = INTERACTIVE_CHOICES_MARKER) -> None:
        self._marker = marker
        self._buffer = ""
        self._seen_marker = False

    def _held_tail_length(self) -> int:
        """Longest suffix of the buffer that is a proper marker prefix."""
        max_k = min(len(self._buffer), len(self._marker) - 1)
        for k in range(max_k, 0, -1):
            if self._marker.startswith(self._buffer[-k:]):
                return k
        return 0

    def feed(self, delta: str) -> str:
        """Consume one raw delta; return the display-safe text."""
        if self._seen_marker:
            # The choices trailer (and anything after it) is never shown.
            return ""
        if not delta:
            return ""
        self._buffer += delta
        marker_pos = self._buffer.find(self._marker)
        if marker_pos != -1:
            self._seen_marker = True
            safe = self._buffer[:marker_pos]
            self._buffer = ""
            return safe
        hold = self._held_tail_length()
        if hold >= len(self._buffer):
            return ""
        # NB: ``self._buffer[:-hold]`` with hold == 0 is ``self._buffer[:0]``
        # (empty!) — the whole buffer would be held back forever and the
        # student would see nothing until finalization. Release everything
        # when there is no marker-prefix tail.
        release = self._buffer[: len(self._buffer) - hold]
        self._buffer = self._buffer[len(release) :]
        return release

    def flush(self) -> str:
        """Release any text still held back at the end of the stream."""
        if self._seen_marker:
            return ""
        out = self._buffer
        self._buffer = ""
        return out
