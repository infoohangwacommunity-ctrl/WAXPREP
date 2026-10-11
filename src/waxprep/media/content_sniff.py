"""Conservative content-type detection from magic bytes.

Never trusts filename or provider MIME alone. Unknown types are marked
unsupported for processing (they may still be stored if policy allows).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SniffResult:
    mime_type: str | None
    media_category: str  # image | audio | document | video | unknown
    supported_for_processing: bool


def sniff_content(data: bytes, claimed_mime: str | None = None) -> SniffResult:
    if not data:
        return SniffResult(None, "unknown", False)
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return SniffResult("image/jpeg", "image", True)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return SniffResult("image/png", "image", True)
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return SniffResult("image/gif", "image", True)
    if head.startswith(b"%PDF"):
        return SniffResult("application/pdf", "document", True)
    if head.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return SniffResult("audio/wav", "audio", True)
    if head.startswith(b"ID3") or head[:2] == b"\xff\xfb":
        return SniffResult("audio/mpeg", "audio", True)
    if head[4:8] == b"ftyp":
        # mp4 / m4a family — store, video not semantically processed
        if b"mp4" in data[4:20] or b"isom" in data[4:20]:
            return SniffResult("video/mp4", "video", False)
        if b"M4A" in data[4:20]:
            return SniffResult("audio/mp4", "audio", True)
    if claimed_mime and claimed_mime.startswith("text/"):
        try:
            data.decode("utf-8")
            return SniffResult("text/plain", "document", True)
        except UnicodeDecodeError:
            pass
    return SniffResult(claimed_mime, "unknown", False)
