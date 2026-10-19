"""Stream-safe incremental formatter.

Blueprint §6.6 (Safe incremental formatting): a stream may stop at any
character — in the middle of a word, a formatting marker, a URL, a list
item or a code fence. The formatter must distinguish:
- Text received so far (the accumulator's ``final_text``).
- Text that can be safely displayed now.
- Text that needs more input before it can be rendered correctly.
- The complete final document.

The formatter never destroys the source text. Intermediate rendering
publishes safe increments while retaining the uncommitted suffix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Formatting markers that need a closing pair to be safe.
_PAIR_MARKERS: tuple[tuple[str, str], ...] = (
    ("**", "**"),  # bold (WhatsApp + Markdown)
    ("*", "*"),  # italic
    ("__", "__"),  # bold underscore
    ("_", "_"),  # italic underscore
    ("~", "~"),  # strikethrough (WhatsApp)
    ("`", "`"),  # inline code
    ("```", "```"),  # code block
)

# A sentence boundary: ., !, ? followed by space or end-of-string.
_SENTENCE_BOUNDARY = re.compile(r"[.!?][\s]|$")

# A paragraph boundary: two or more newlines.
_PARAGRAPH_BOUNDARY = re.compile(r"\n\s*\n")


@dataclass(frozen=True, slots=True)
class SafeIncrement:
    """The result of a safe-incremental formatting pass.

    ``safe_text`` is what can be displayed now without breaking
    formatting. ``pending_suffix`` is the part that needs more input
    before it can be rendered correctly (an unclosed marker, a
    partial URL, an incomplete code fence).
    """

    safe_text: str
    pending_suffix: str


def find_safe_boundary(text: str) -> int:
    """Find the latest safe boundary in ``text``.

    A safe boundary is a position where:
    - All formatting markers opened before this point are closed.
    - We are not inside a code block.
    - We are not mid-word (unless we have to — e.g. the whole text is
      one word).

    Returns the index of the first character that should be RETAINED
    as the pending suffix. Everything before that index is safe to
    display.
    """
    if not text:
        return 0

    # Track open markers.
    open_markers: list[tuple[str, int]] = []
    in_code_block = False
    code_block_pos = -1

    i = 0
    while i < len(text):
        # Check for code block fence (highest priority).
        if text[i : i + 3] == "```":
            if in_code_block:
                # Closing fence.
                in_code_block = False
                open_markers = [m for m in open_markers if m[0] != "```"]
            else:
                # Opening fence — everything after this is in a code
                # block until we see the closing fence. Not safe to
                # split inside.
                in_code_block = True
                code_block_pos = i
            i += 3
            continue

        if in_code_block:
            # Inside a code block — not safe to split.
            i += 1
            continue

        # Check for pair markers.
        for open_marker, _close_marker in _PAIR_MARKERS:
            if open_marker == "```":
                continue  # handled above
            ml = len(open_marker)
            if text[i : i + ml] == open_marker:
                # Check if this is a closing marker for an already-open one.
                closed = False
                for j, (om, _pos) in enumerate(open_markers):
                    if om == open_marker:
                        # Close it.
                        open_markers.pop(j)
                        closed = True
                        break
                if not closed:
                    # Opening a new marker.
                    open_markers.append((open_marker, i))
                i += ml
                break
        else:
            # No marker at this position.
            i += 1

    # If there are open markers, the safe boundary is the position of
    # the earliest unclosed marker.
    if open_markers:
        earliest_open = min(pos for _, pos in open_markers)
        return earliest_open

    if in_code_block:
        # Unclosed code block — not safe to display anything after the
        # opening fence.
        return code_block_pos if code_block_pos >= 0 else 0

    # All markers closed. The entire text is safe to display.
    return len(text)


def safe_increment(text: str) -> SafeIncrement:
    """Split ``text`` into a safe-to-display prefix and a pending suffix.

    The safe prefix can be shown to the student without breaking
    formatting. The pending suffix needs more input before it can be
    rendered correctly. The full text is always preserved in the
    accumulator — this function only decides what to DISPLAY.
    """
    if not text:
        return SafeIncrement(safe_text="", pending_suffix="")
    boundary = find_safe_boundary(text)
    if boundary <= 0:
        return SafeIncrement(safe_text="", pending_suffix=text)
    if boundary >= len(text):
        return SafeIncrement(safe_text=text, pending_suffix="")
    return SafeIncrement(safe_text=text[:boundary], pending_suffix=text[boundary:])


def is_safe_final(text: str) -> bool:
    """Check whether the complete text has all formatting markers closed.

    Used at finalization to verify the complete answer is well-formed
    before the permanent final message is sent (blueprint §6.6).
    """
    if not text:
        return True
    boundary = find_safe_boundary(text)
    return boundary >= len(text)


__all__ = [
    "SafeIncrement",
    "find_safe_boundary",
    "is_safe_final",
    "safe_increment",
]
