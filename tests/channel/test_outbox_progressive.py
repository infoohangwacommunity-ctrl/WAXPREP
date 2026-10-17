"""Outbox progressive splitting is infrastructure-only (no educational branches)."""

from waxprep.channel.whatsapp.outbox import split_text_for_delivery


def test_short_text_one_chunk() -> None:
    assert split_text_for_delivery("Hello") == ["Hello"]


def test_long_text_splits_without_losing_content() -> None:
    body = "Paragraph one.\n\n" + ("word " * 200)
    chunks = split_text_for_delivery(body, soft_limit=80)
    assert len(chunks) >= 2
    joined = " ".join(chunks)
    assert "Paragraph one" in joined
    assert "word" in joined
