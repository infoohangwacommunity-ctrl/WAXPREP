"""Artifact processors: document parse, STT, diagram render."""

from waxprep.media.processors.diagram import (
    DiagramSpec,
    render_diagram_svg,
    validate_diagram_spec,
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
    "MockDocumentProcessor",
    "MockSpeechToText",
    "SpeechToText",
    "TranscriptResult",
    "UpstageDocumentProcessor",
    "render_diagram_svg",
    "validate_diagram_spec",
]
