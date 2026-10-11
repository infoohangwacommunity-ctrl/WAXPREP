"""Configurable size and cost caps enforced before external calls."""

from __future__ import annotations

from waxprep.settings import MediaLimits


class MediaLimitError(Exception):
    """A configured media limit would be exceeded; do not call the provider."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def enforce_inbound_size(size: int, limits: MediaLimits) -> None:
    if size < 0:
        raise MediaLimitError("invalid_size", "negative size")
    if size > limits.max_inbound_bytes:
        raise MediaLimitError(
            "inbound_too_large",
            f"payload {size} bytes exceeds limit {limits.max_inbound_bytes}",
        )


def enforce_audio_size(size: int, limits: MediaLimits) -> None:
    if size > limits.max_audio_bytes:
        raise MediaLimitError(
            "audio_too_large",
            f"audio {size} bytes exceeds limit {limits.max_audio_bytes}",
        )
