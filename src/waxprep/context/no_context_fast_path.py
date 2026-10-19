"""Mechanical message checks — not semantic classification.

Context Intelligence decides whether investigation is useful.
This module must never classify student intent by vocabulary.
"""

from __future__ import annotations


def is_blank_message(text: str) -> bool:
    """True only when the message has no non-whitespace content.

    Empty input is mechanical infrastructure, not meaning analysis.
    Non-empty messages — including greetings — are left to Context Intelligence.
    """
    return not text.strip()


# Deprecated name kept only so accidental imports fail loudly in tests.
def can_use_no_context_fast_path(text: str) -> bool:
    """Do not use for semantic routing.

    Preserved as an alias that only detects blank messages so any caller
    that still invokes the old name cannot skip CI based on greetings.
    """
    return is_blank_message(text)
