"""Mechanical blank-message check — not semantic classification."""

from waxprep.context.no_context_fast_path import is_blank_message


def test_blank_message_is_mechanical() -> None:
    assert is_blank_message("") is True
    assert is_blank_message("   ") is True
    assert is_blank_message("\n\t") is True


def test_non_blank_is_never_classified_by_vocabulary() -> None:
    """Any non-empty text is not treated as 'safe to skip CI' by phrase lists."""
    samples = (
        "hello",
        "thanks",
        "ok",
        "yes",
        "Remember the assignment?",
        "Where were we?",
        "Can we pick up that red thing from before?",
    )
    for text in samples:
        assert is_blank_message(text) is False
