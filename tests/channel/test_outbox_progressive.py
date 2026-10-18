"""Outbox progressive splitting is infrastructure-only (no educational branches)."""

from waxprep.channel.whatsapp.outbox import split_text_for_delivery


def test_short_text_one_chunk() -> None:
    assert split_text_for_delivery("Hello") == ["Hello"]


def test_long_text_splits_without_losing_content() -> None:
    # The splitter only splits at the platform hard limit (4000 chars for
    # WhatsApp). ``soft_limit`` is ignored (kept for call-site compatibility).
    # Build a body that actually exceeds the platform max so the splitter
    # exercises its split path; otherwise the test would always return one
    # chunk and the assertion `len(chunks) >= 2` would silently regress.
    body = "Paragraph one.\n\n" + ("word " * 1000)  # ~5016 chars > 4000 limit
    chunks = split_text_for_delivery(body, soft_limit=80)
    assert len(chunks) >= 2
    joined = " ".join(chunks)
    assert "Paragraph one" in joined
    assert "word" in joined
