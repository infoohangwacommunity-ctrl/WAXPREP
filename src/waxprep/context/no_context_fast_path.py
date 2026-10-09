"""Cheap path for messages that do not need CI investigation."""

from __future__ import annotations

_GREETINGS = frozenset(
    {
        "hello",
        "hi",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "thanks",
        "thank you",
        "ok",
        "okay",
        "yes",
        "no",
    }
)


def can_use_no_context_fast_path(text: str) -> bool:
    """Return True when a full CI investigation is unlikely to help."""
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return True
    if len(normalized) > 40:
        return False
    return normalized in _GREETINGS
