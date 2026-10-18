"""Platform-aware multi-message delivery helpers.

Split only when over the platform maximum. Prefer complete paragraphs /
sentences so students see natural tutor bubbles — not fixed 700/900 cuts.
"""

from __future__ import annotations

# Hard ceilings from each platform (safety only, not teaching policy).
PLATFORM_MAX_CHARS = {
    "whatsapp": 4000,  # Cloud API practical limit for text body
    "telegram": 4096,  # Bot API sendMessage limit
}


def platform_max(channel: str) -> int:
    return PLATFORM_MAX_CHARS.get((channel or "").lower(), 4000)


def split_for_platform(text: str, channel: str) -> list[str]:
    """Split tutor text into complete messages for a channel.

    - Under the platform max → one message.
    - Over max → split on blank lines, then sentence ends, then spaces.
    Never invent educational structure; only respect transport limits.
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = platform_max(channel)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            piece = remaining.strip()
            if piece:
                chunks.append(piece)
            break
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < max(40, limit // 4):
            cut = window.rfind(". ")
            if cut >= max(40, limit // 4):
                cut = cut + 1
        if cut < max(40, limit // 4):
            cut = window.rfind("! ")
            if cut >= max(40, limit // 4):
                cut = cut + 1
        if cut < max(40, limit // 4):
            cut = window.rfind("? ")
            if cut >= max(40, limit // 4):
                cut = cut + 1
        if cut < max(40, limit // 4):
            cut = window.rfind("\n")
        if cut < max(40, limit // 4):
            cut = window.rfind(" ")
        if cut < max(40, limit // 4):
            cut = limit
        piece = remaining[:cut].strip()
        remaining = remaining[cut:].lstrip()
        if piece:
            chunks.append(piece)
    return chunks or [text]


__all__ = ["PLATFORM_MAX_CHARS", "platform_max", "split_for_platform"]
