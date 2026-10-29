"""Live voice-note transcription via the Gemini API.

Implements the 'SpeechToText' Protocol for inline (webhook-path)
transcription so a student's voice note drives the tutor turn with
their actual words. Explicit provider selection only — no silent
failover between paid providers.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from waxprep.media.processors.stt import TranscriptResult
from waxprep.settings import Settings

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-flash-latest"
_TRANSCRIBE_PROMPT = (
    "Transcribe this voice note exactly as spoken, in its original "
    "language. Fix only obvious stutters and disfluencies. Return only "
    "the transcript text."
)


class GeminiSpeechToText:
    """SpeechToText backed by Gemini 'generateContent' (inline audio)."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = (model or "").strip() or _DEFAULT_MODEL
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return "gemini"

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None
    ) -> TranscriptResult:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent"
        )
        payload: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {"text": _TRANSCRIBE_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": mime_type or "audio/ogg",
                                "data": base64.b64encode(audio).decode("ascii"),
                            }
                        },
                    ]
                }
            ]
        }
        close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds)
            close = True
        try:
            resp = await client.post(
                url,
                headers={
                    "x-goog-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or [{}]
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = " ".join(str(part.get("text") or "") for part in parts).strip()
            return TranscriptResult(
                text=text,
                provider="gemini",
                model=self._model,
                confidence=None,
            )
        finally:
            if close:
                await client.aclose()


def build_speech_to_text(settings: Settings) -> GeminiSpeechToText | None:
    """Compose the configured STT backend, or None when unavailable.

    WAXPREP_VOICE_PROVIDER=gemini plus GEMINI_API_KEY enables Gemini
    transcription; WAXPREP_VOICE_MODEL overrides the model (default
    gemini-flash-latest). Any other or unset provider keeps inline voice
    transcription off — the durable ingest_media path stays the fallback
    and the handler never blocks on STT.
    """
    provider = (getattr(settings, "voice_provider", None) or "").strip().lower()
    if provider != "gemini":
        if provider:
            logger.info(
                "voice transcription unsupported for provider %r; staying off",
                provider,
            )
        return None
    api_key = getattr(settings, "gemini_api_key", None)
    if not api_key:
        logger.warning(
            "WAXPREP_VOICE_PROVIDER=gemini but GEMINI_API_KEY is unset; "
            "voice notes will not be transcribed inline"
        )
        return None
    return GeminiSpeechToText(
        api_key,
        model=os.environ.get("WAXPREP_VOICE_MODEL"),
    )


__all__ = ["GeminiSpeechToText", "build_speech_to_text"]
