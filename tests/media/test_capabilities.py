from waxprep.media.capabilities import (
    UNSUPPORTED_IMAGE_GENERATION,
    UNSUPPORTED_VIDEO_UNDERSTANDING,
)


def test_capability_messages_are_honest() -> None:
    assert "not configured" in UNSUPPORTED_IMAGE_GENERATION.lower()
    assert "video" in UNSUPPORTED_VIDEO_UNDERSTANDING.lower()
