"""Optional speech-to-text processors (Gemini or Grok only).

Not Teachers. Explicit provider selection via configuration.
No silent failover between paid providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    provider: str
    model: str
    confidence: float | None = None
    word_timestamps: tuple[tuple[str, float, float], ...] = ()


class SpeechToText(Protocol):
    @property
    def name(self) -> str: ...

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None
    ) -> TranscriptResult: ...


class MockSpeechToText:
    def __init__(self, text: str = "hello student", provider: str = "mock") -> None:
        self._text = text
        self._provider = provider

    @property
    def name(self) -> str:
        return self._provider

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None
    ) -> TranscriptResult:
        return TranscriptResult(
            text=self._text,
            provider=self._provider,
            model="mock-stt",
            confidence=0.99,
        )


class UnavailableSpeechToText:
    """When no voice provider is configured."""

    @property
    def name(self) -> str:
        return "unavailable"

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None
    ) -> TranscriptResult:
        raise RuntimeError(
            "voice transcription is not configured; student may send text instead"
        )
