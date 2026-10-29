"""Live voice-note transcription via Groq Whisper.

OpenAI-compatible /audio/transcriptions on api.groq.com. Explicit
provider selection only — no silent failover to Gemini or other paid APIs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from waxprep.media.processors.stt import TranscriptResult
from waxprep.settings import Settings

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "whisper-large-v3-turbo"
_BASE = "https://api.groq.com/openai/v1"


class GroqWhisperSpeechToText:
    """SpeechToText backed by Groq Whisper (multipart audio upload)."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._model = (model or "").strip() or _DEFAULT_MODEL
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return "groq"

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None
    ) -> TranscriptResult:
        if not audio:
            raise ValueError("cannot transcribe empty audio")
        # WhatsApp voice notes are typically audio/ogg; Groq accepts common
        # containers. Filename extension helps the API pick a decoder.
        ext = "ogg"
        mt = (mime_type or "").lower()
        if "mpeg" in mt or "mp3" in mt:
            ext = "mp3"
        elif "wav" in mt or "wave" in mt:
            ext = "wav"
        elif "mp4" in mt or "m4a" in mt:
            ext = "m4a"
        elif "webm" in mt:
            ext = "webm"

        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            resp = await client.post(
                f"{_BASE}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": (f"audio.{ext}", audio, mime_type or f"audio/{ext}")},
                data={"model": self._model, "response_format": "json"},
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError("groq whisper request timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("groq whisper transport failed") from exc
        finally:
            if owns:
                await client.aclose()

        if resp.status_code >= 400:
            raise RuntimeError(
                f"groq whisper error {resp.status_code}: {resp.text[:200]}"
            )
        data: dict[str, Any] = resp.json()
        text = str(data.get("text") or "").strip()
        return TranscriptResult(
            text=text,
            provider="groq",
            model=self._model,
            confidence=None,
        )


def build_groq_speech_to_text(settings: Settings) -> GroqWhisperSpeechToText | None:
    """Return Groq Whisper STT when WAXPREP_VOICE_PROVIDER=groq and key set."""
    provider = (getattr(settings, "voice_provider", None) or "").strip().lower()
    if provider != "groq":
        return None
    api_key = getattr(settings, "groq_api_key", None)
    if not api_key:
        logger.info("WAXPREP_VOICE_PROVIDER=groq but GROQ_API_KEY missing; STT off")
        return None
    model = getattr(settings, "voice_model", None)
    return GroqWhisperSpeechToText(api_key=api_key, model=model)
