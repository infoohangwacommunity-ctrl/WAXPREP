"""Platform-aware multi-message delivery helpers.

Split only when over the platform maximum. Prefer complete paragraphs /
sentences so students see natural tutor bubbles — not fixed 700/900 cuts.

Accessibility (WCAG 1.3.1 Info and Relationships, 1.3.2 Meaningful
Sequence): a numbered list is a structural unit for weak-foundation
students. The splitter never cuts INSIDE a numbered list — it either
moves the whole list to the next bubble (when the list would straddle
the transport limit) or, when the list itself exceeds the limit, splits
BETWEEN items so every bubble contains complete numbered steps.
"""

from __future__ import annotations

import re

# Hard ceilings from each platform (safety only, not teaching policy).
PLATFORM_MAX_CHARS = {
    "whatsapp": 4000,  # Cloud API practical limit for text body
    "telegram": 4096,  # Bot API sendMessage limit
}


def platform_max(channel: str) -> int:
    return PLATFORM_MAX_CHARS.get((channel or "").lower(), 4000)


# ---------------------------------------------------------------------------
# Numbered-list detection (markdown ordered lists: `^\s*\d+\.\s+`)
# ---------------------------------------------------------------------------
#
# A numbered-list run is a maximal sequence of consecutive non-blank lines
# that contains at least one line starting with `\d+\.\s+`. A blank line
# ends a run. Continuation lines (non-list lines immediately following a
# list item, with no blank line between them) are part of the current
# run — this is intentionally permissive so a multi-line item such as
#
#     1. First, do this.
#        Then do that.
#     2. Next, ...
#
# is treated as one indivisible run rather than two separate runs.
#
# The splitter uses these runs to (a) refuse to cut inside a run, and
# (b) when a run is itself too long, fall back to cutting at item
# boundaries (`\n` immediately before a `\d+\.\s+` line).

_NUMBERED_ITEM_START_RE = re.compile(r"^[ \t]*\d+\.[ \t]+", re.MULTILINE)
_LIST_ITEM_BOUNDARY_RE = re.compile(r"\n(?=[ \t]*\d+\.[ \t]+)")


def _find_numbered_list_runs(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans of numbered-list runs in ``text``.

    A run is a maximal sequence of consecutive non-blank lines that
    contains at least one line starting with ``\\d+\\.\\s+``. A blank
    line ends a run. The span is half-open: ``[start, end)`` — ``end``
    is the offset of the first blank line (or end-of-text) after the
    run, so the run's text is ``text[start:end]``.
    """
    runs: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        # Skip whitespace-only lines.
        while pos < n and text[pos] in " \t\n\r":
            pos += 1
        if pos >= n:
            break
        run_start = pos
        has_list_item = False
        line_start = pos
        while line_start < n:
            nl = text.find("\n", line_start)
            if nl == -1:
                line_end = n
            else:
                line_end = nl
            line = text[line_start:line_end]
            if line.strip() == "":
                # Blank line ends the run.
                break
            if _NUMBERED_ITEM_START_RE.match(line):
                has_list_item = True
            if nl == -1:
                line_start = n
            else:
                line_start = nl + 1
        run_end = line_start
        if has_list_item:
            runs.append((run_start, run_end))
        pos = line_start
    return runs


def _adjust_cut_past_list(
    remaining: str,
    cut: int,
    base_offset: int,
    runs: list[tuple[int, int]],
) -> int:
    """If ``cut`` lands strictly inside a numbered-list run, move it.

    * If the run starts BEFORE the current chunk (``start < base_offset``)
      we cannot move the cut before the run — the run already started in
      this chunk. Instead, fall back to the last item boundary at or
      before ``cut`` so we split BETWEEN items (each bubble keeps whole
      numbered steps), never mid-item.
    * If the run starts inside the current chunk (``start >= base_offset``)
      move the cut to the START of the run, so the whole list moves to
      the next bubble.
    * If the cut is not inside any run, return it unchanged.
    """
    if not runs:
        return cut
    abs_cut = base_offset + cut
    for start, end in runs:
        if start < abs_cut < end:
            if start <= base_offset:
                # The run started in this chunk. Split BETWEEN items:
                # find the last `\n` before a `\d+\.\s+` line that lies
                # at or before `cut`, so the cut lands between two items.
                sub = remaining[:cut]
                boundaries = [
                    m.start() for m in _LIST_ITEM_BOUNDARY_RE.finditer(sub)
                ]
                if boundaries:
                    return boundaries[-1]
                # No item boundary before cut (e.g. a single very long
                # item). Cannot avoid a mid-item split — keep the cut.
                return cut
            # The run starts inside this chunk: move the cut to just
            # before the run so the whole list goes to the next bubble.
            return start - base_offset
    return cut


def split_for_platform(text: str, channel: str) -> list[str]:
    """Split tutor text into complete messages for a channel.

    - Under the platform max → one message.
    - Over max → split on blank lines, then sentence ends, then spaces.
    - **Never split inside a numbered list** (WCAG 1.3.1 / 1.3.2): if a
      numbered list would straddle the transport limit, the whole list
      is moved to the next bubble. If a numbered list alone exceeds the
      limit, the split lands BETWEEN items (each bubble contains whole
      numbered steps, never a truncated ``"2. Do"`` fragment).
    - Never invent educational structure; only respect transport limits.
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = platform_max(channel)
    if len(text) <= limit:
        return [text]

    runs = _find_numbered_list_runs(text)

    chunks: list[str] = []
    remaining = text
    base_offset = 0
    min_cut = max(40, limit // 4)
    while remaining:
        if len(remaining) <= limit:
            piece = remaining.strip()
            if piece:
                chunks.append(piece)
            break
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < min_cut:
            cut = window.rfind(". ")
            if cut >= min_cut:
                cut = cut + 1
        if cut < min_cut:
            cut = window.rfind("! ")
            if cut >= min_cut:
                cut = cut + 1
        if cut < min_cut:
            cut = window.rfind("? ")
            if cut >= min_cut:
                cut = cut + 1
        if cut < min_cut:
            cut = window.rfind("\n")
        if cut < min_cut:
            cut = window.rfind(" ")
        if cut < min_cut:
            cut = limit

        # Accessibility: never split inside a numbered-list run. If the
        # cut would land inside a list, move it to the start of the run
        # (whole list → next bubble) or, when the run already started in
        # this chunk, to the last item boundary before the cut (split
        # between items, each bubble keeps whole numbered steps).
        cut = _adjust_cut_past_list(remaining, cut, base_offset, runs)

        if cut <= 0:
            # The run starts at the very beginning of `remaining` AND
            # no item boundary exists before `limit`. The list itself
            # is longer than the limit and the first item is longer
            # than the limit — a degenerate case. Force a hard cut at
            # the limit so we make progress (the item will be split,
            # but we have exhausted the between-items option).
            cut = limit

        piece = remaining[:cut].strip()
        remaining = remaining[cut:].lstrip()
        base_offset += cut
        if piece:
            chunks.append(piece)
        else:
            # Safety: never loop forever. If somehow the cut produced
            # an empty piece, force-advance by the limit.
            if not remaining:
                break
            piece = remaining[:limit].strip()
            remaining = remaining[limit:].lstrip()
            base_offset += limit
            if piece:
                chunks.append(piece)
    return chunks or [text]


__all__ = ["PLATFORM_MAX_CHARS", "platform_max", "split_for_platform"]
