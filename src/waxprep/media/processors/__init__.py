"""Artifact processors: document parse, STT, diagram render."""

from waxprep.media.processors.diagram import (
    DiagramSpec,
    render_diagram_svg,
    validate_diagram_spec,
)
from waxprep.media.processors.gemini_stt import (
    GeminiSpeechToText,
    build_speech_to_text,
)
from waxprep.media.processors.stt import (
    MockSpeechToText,
    SpeechToText,
    TranscriptResult,
)
from waxprep.media.processors.upstage_parse import (
    MockDocumentProcessor,
    UpstageDocumentProcessor,
)

__all__ = [
    "DiagramSpec",
    "GeminiSpeechToText",
    "MockDocumentProcessor",
    "MockSpeechToText",
    "SpeechToText",
    "TranscriptResult",
    "UpstageDocumentProcessor",
    "build_speech_to_text",
    "render_diagram_svg",
    "validate_diagram_spec",
]
