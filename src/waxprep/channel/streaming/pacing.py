"""Shared pacing policy for streaming delivery.

Blueprint §8: the pacing policy balances responsiveness, readability,
network traffic and platform limits. It never makes educational
decisions ("short answers use WhatsApp mode" is forbidden).

The policy is configurable and reusable across providers and channels.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PacingDecision:
    """The pacing policy's verdict on whether to send an update now.

    ``should_update`` is True when enough new text has accumulated OR
    a maximum interval has elapsed OR a safe boundary is available OR
    the response is about to finish.
    """

    should_update: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PacingPolicy:
    """Configurable pacing thresholds. Infrastructure-only — no
    educational rules.

    Defaults are conservative; tune from measured behaviour
    (blueprint §8.2: "The exact thresholds should be tuned after tests").
    """

    # Minimum new characters since the last update before sending another.
    # Prevents sending every token.
    min_chars_per_update: int = 80

    # Maximum time between updates when new text is available.
    # Prevents starvation.
    max_interval_seconds: float = 3.0

    # Maximum update frequency per recipient. Prevents rate-limiting.
    min_interval_seconds: float = 0.5

    # Whether paragraph boundaries trigger an immediate update.
    flush_on_paragraph: bool = True

    # Whether sentence boundaries trigger an immediate update.
    flush_on_sentence: bool = False

    def should_send_update(
        self,
        *,
        new_chars: int,
        elapsed_since_last: float,
        has_safe_boundary: bool,
        is_final: bool,
    ) -> PacingDecision:
        """Decide whether to send an update now.

        Inputs (blueprint §8.1):
        - new_chars: how much new text has accumulated since the last update.
        - elapsed_since_last: seconds since the last update was sent.
        - has_safe_boundary: whether a safe sentence or paragraph boundary
          is available.
        - is_final: whether the response is about to finish.

        Never makes semantic decisions about what the student needs to learn.
        """
        if is_final:
            return PacingDecision(True, "final")
        if new_chars == 0:
            return PacingDecision(False, "no new text")
        if elapsed_since_last < self.min_interval_seconds:
            return PacingDecision(False, "rate limited")
        if has_safe_boundary and self.flush_on_paragraph and new_chars >= 40:
            return PacingDecision(True, "paragraph boundary")
        if has_safe_boundary and self.flush_on_sentence and new_chars >= 40:
            return PacingDecision(True, "sentence boundary")
        if new_chars >= self.min_chars_per_update:
            return PacingDecision(True, "enough new text")
        if elapsed_since_last >= self.max_interval_seconds:
            return PacingDecision(True, "max interval elapsed")
        return PacingDecision(False, "not enough new text")


# Default policy instances for common channel types.
WHATSAPP_DEFAULT = PacingPolicy(
    min_chars_per_update=200,
    max_interval_seconds=5.0,
    min_interval_seconds=1.0,
    flush_on_paragraph=False,  # don't auto-split on paragraphs
    flush_on_sentence=False,
)

TELEGRAM_DRAFT_DEFAULT = PacingPolicy(
    min_chars_per_update=40,
    max_interval_seconds=2.0,
    min_interval_seconds=0.5,
    flush_on_paragraph=True,
    flush_on_sentence=True,
)


__all__ = [
    "PacingDecision",
    "PacingPolicy",
    "TELEGRAM_DRAFT_DEFAULT",
    "WHATSAPP_DEFAULT",
]
