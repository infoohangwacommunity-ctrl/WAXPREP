"""Honest capability responses for unsupported modalities."""

from __future__ import annotations

UNSUPPORTED_IMAGE_GENERATION = (
    "Image generation is not configured. I can help with text explanations "
    "or a structured educational diagram if that would help."
)

UNSUPPORTED_VIDEO_UNDERSTANDING = (
    "I received your video, but video understanding is not available yet. "
    "You can describe what you need in text or send a photo or document."
)

VOICE_UNAVAILABLE = (
    "Voice notes are saved, but transcription is not configured right now. "
    "Please send your question as text and I will help."
)

PROCESSING_QUEUED = (
    "I received your file and am processing it. Ask again shortly and I will "
    "use what was extracted."
)
