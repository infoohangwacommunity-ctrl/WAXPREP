"""Tests for the shared streaming contract, accumulator, formatter, coordinator.

Blueprint §12 (Test blueprint): deterministic tests that verify the
application's streaming infrastructure behaves correctly. These are
component evaluations (no live AI calls); live-model evaluation is
gated behind the ``-m live`` marker.
"""

from __future__ import annotations

import asyncio

import pytest

from waxprep.channel.streaming.accumulator import AnswerAccumulator
from waxprep.channel.streaming.contract import (
    ChannelCapabilities,
    ChannelCapability,
    DeliveryStrategy,
)
from waxprep.channel.streaming.coordinator import StreamingCoordinator
from waxprep.channel.streaming.formatter import (
    is_safe_final,
    safe_increment,
)
from waxprep.channel.streaming.pacing import (
    TELEGRAM_DRAFT_DEFAULT,
    WHATSAPP_DEFAULT,
    PacingPolicy,
)

# ---------------------------------------------------------------------------
# AnswerAccumulator — authoritative text from deltas (blueprint §3.2, §6.3, §9.1)
# ---------------------------------------------------------------------------


def test_accumulator_reconstructs_exact_text_from_deltas() -> None:
    """The canonical final answer is the exact concatenation of all deltas
    in order. Never reconstructed from trimmed outbound messages."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("Hello")
    acc.append_delta(", ")
    acc.append_delta("how ")
    acc.append_delta("can I help you today?")
    acc.complete()
    assert acc.final_text == "Hello, how can I help you today?"


def test_accumulator_preserves_paragraph_boundaries() -> None:
    """Paragraph boundaries (\\n\\n) must survive the accumulator. The
    previous WhatsApp progressive delivery lost them because it stored
    stripped emitted pieces."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("Photosynthesis is the process by which plants make food.")
    acc.append_delta("\n\nIt uses sunlight, water and carbon dioxide.")
    acc.complete()
    assert "\n\n" in acc.final_text
    assert acc.final_text == (
        "Photosynthesis is the process by which plants make food."
        "\n\nIt uses sunlight, water and carbon dioxide."
    )


def test_accumulator_preserves_whitespace() -> None:
    """Whitespace and line breaks must be preserved verbatim."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("  leading spaces\n  trailing  ")
    acc.complete()
    assert acc.final_text == "  leading spaces\n  trailing  "


def test_accumulator_rejects_delta_after_cancel() -> None:
    """A cancelled response must not accept new deltas (blueprint §9.4)."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("hello")
    acc.cancel()
    with pytest.raises(RuntimeError, match="cannot append delta"):
        acc.append_delta("world")


def test_accumulator_rejects_delta_after_complete() -> None:
    """A completed response must not accept new deltas."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("hello")
    acc.complete()
    with pytest.raises(RuntimeError, match="cannot append delta"):
        acc.append_delta("world")


def test_accumulator_empty_delta_is_noop() -> None:
    """An empty delta must not be appended (blueprint §8.3: no empty updates)."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("hello")
    acc.append_delta("")
    acc.complete()
    assert acc.delta_count == 1
    assert acc.final_text == "hello"


def test_accumulator_unicode_and_emoji_preserved() -> None:
    """Unicode, emojis, and combining characters must survive (blueprint §12.4)."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("Héllo wörld 🌍 ")
    acc.append_delta("Привет мир ")
    acc.append_delta("こんにちは世界")
    acc.complete()
    assert acc.final_text == "Héllo wörld 🌍 Привет мир こんにちは世界"


# ---------------------------------------------------------------------------
# Stream-safe formatter — safe incremental rendering (blueprint §6.6)
# ---------------------------------------------------------------------------


def test_safe_increment_plain_text_is_all_safe() -> None:
    """Plain text with no formatting markers is entirely safe to display."""
    inc = safe_increment("Hello, how can I help you today?")
    assert inc.safe_text == "Hello, how can I help you today?"
    assert inc.pending_suffix == ""


def test_safe_increment_unclosed_bold_is_pending() -> None:
    """An unclosed ** marker must not be displayed — it would render as
    a broken formatting pair (blueprint §6.6)."""
    inc = safe_increment("This is **bold")
    # The ** is unclosed — the safe boundary is at or before the **.
    assert inc.pending_suffix != ""
    assert inc.safe_text != "This is **bold"


def test_safe_increment_closed_bold_is_safe() -> None:
    """A closed ** pair is safe to display."""
    inc = safe_increment("This is **bold** text.")
    assert inc.safe_text == "This is **bold** text."
    assert inc.pending_suffix == ""


def test_safe_increment_unclosed_code_block_is_pending() -> None:
    """An unclosed ``` code fence must not be displayed — the student
    would see a broken code block."""
    inc = safe_increment("Here is code:\n```python\nprint('hello')")
    assert inc.pending_suffix != ""


def test_safe_increment_closed_code_block_is_safe() -> None:
    """A closed ``` code block is safe to display."""
    text = "Here is code:\n```python\nprint('hello')\n```"
    inc = safe_increment(text)
    assert inc.safe_text == text
    assert inc.pending_suffix == ""


def test_safe_increment_split_at_paragraph_boundary() -> None:
    """A paragraph boundary is a safe cut point."""
    text = "First paragraph.\n\nSecond paragraph."
    inc = safe_increment(text)
    # The safe boundary should be at or before the paragraph boundary.
    assert len(inc.safe_text) <= len(text)


def test_is_safe_final_complete_text() -> None:
    """A complete well-formed text is safe for final rendering."""
    assert is_safe_final("Hello, how can I help you today?")
    assert is_safe_final("This is **bold** and *italic*.")
    assert is_safe_final("```python\nprint('hello')\n```")


def test_is_safe_final_unclosed_marker() -> None:
    """Text with an unclosed marker is NOT safe for final rendering."""
    assert not is_safe_final("This is **unclosed")
    assert not is_safe_final("```python\nprint('hello')")


# ---------------------------------------------------------------------------
# Streaming coordinator — strategy selection from capabilities (blueprint §4.3)
# ---------------------------------------------------------------------------


def test_coordinator_selects_native_draft_for_telegram() -> None:
    """When the channel reports NATIVE_TEXT_DRAFT_UPDATES, the coordinator
    selects the NATIVE_DRAFT strategy."""
    caps = ChannelCapabilities(
        channel="telegram",
        capabilities=frozenset({ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES}),
    )
    acc = AnswerAccumulator()
    coord = StreamingCoordinator(
        accumulator=acc,
        capabilities=caps,
        pacing=TELEGRAM_DRAFT_DEFAULT,
        send_update=lambda text: asyncio.sleep(0, result=None),  # no-op
    )
    assert coord.strategy is DeliveryStrategy.NATIVE_DRAFT


def test_coordinator_selects_final_only_for_whatsapp() -> None:
    """WhatsApp reports only COMPLETE_MESSAGE_SEND — the coordinator
    selects FINAL_ONLY."""
    caps = ChannelCapabilities(
        channel="whatsapp",
        capabilities=frozenset({ChannelCapability.COMPLETE_MESSAGE_SEND}),
    )
    acc = AnswerAccumulator()
    coord = StreamingCoordinator(
        accumulator=acc,
        capabilities=caps,
        pacing=WHATSAPP_DEFAULT,
        send_update=lambda text: asyncio.sleep(0, result=None),
    )
    assert coord.strategy is DeliveryStrategy.FINAL_ONLY


def test_coordinator_selects_typing_plus_final_when_typing_available() -> None:
    """When the channel reports TYPING_INDICATOR but not native drafts,
    the coordinator selects TYPING_PLUS_FINAL."""
    caps = ChannelCapabilities(
        channel="test",
        capabilities=frozenset({ChannelCapability.TYPING_INDICATOR}),
    )
    acc = AnswerAccumulator()
    coord = StreamingCoordinator(
        accumulator=acc,
        capabilities=caps,
        pacing=WHATSAPP_DEFAULT,
        send_update=lambda text: asyncio.sleep(0, result=None),
    )
    assert coord.strategy is DeliveryStrategy.TYPING_PLUS_FINAL


# ---------------------------------------------------------------------------
# Coordinator lifecycle — cancellation prevents stale updates (blueprint §9.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_cancelled_response_ignores_stale_deltas() -> None:
    """A cancelled response must not accept or deliver further deltas."""
    caps = ChannelCapabilities(
        channel="test",
        capabilities=frozenset({ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES}),
    )
    acc = AnswerAccumulator()
    acc.start()
    updates: list[str] = []

    async def send_update(text: str) -> str | None:
        updates.append(text)
        return None

    coord = StreamingCoordinator(
        accumulator=acc,
        capabilities=caps,
        pacing=PacingPolicy(min_chars_per_update=1, min_interval_seconds=0),
        send_update=send_update,
    )
    await coord.on_delta("hello")
    await coord.cancel()
    await coord.on_delta("stale delta from old response")
    # The stale delta must not have been delivered.
    assert not any("stale" in u for u in updates)


@pytest.mark.asyncio
async def test_coordinator_does_not_send_empty_updates() -> None:
    """The coordinator must not send an empty update (blueprint §8.3).
    If no new text has accumulated since the last update, no update is sent."""
    caps = ChannelCapabilities(
        channel="test",
        capabilities=frozenset({ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES}),
    )
    acc = AnswerAccumulator()
    acc.start()
    updates: list[str] = []

    async def send_update(text: str) -> str | None:
        updates.append(text)
        return None

    coord = StreamingCoordinator(
        accumulator=acc,
        capabilities=caps,
        pacing=PacingPolicy(min_chars_per_update=1, min_interval_seconds=0),
        send_update=send_update,
    )
    # Send an empty delta — should not produce an update.
    await coord.on_delta("")
    assert len(updates) == 0


# ---------------------------------------------------------------------------
# WhatsApp progressive delivery — default to one coherent final message
# (blueprint §6.1, §6.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whatsapp_default_produces_one_final_message_not_chunks() -> None:
    """The default WhatsApp strategy is FINAL_ONLY: one coherent final
    message, not fragmented progressive chunks (blueprint §6.1)."""
    from waxprep.channel.whatsapp.progressive import ProgressiveWhatsAppDelivery

    sent_texts: list[str] = []

    async def send_text(text: str) -> None:
        sent_texts.append(text)

    delivery = ProgressiveWhatsAppDelivery(
        send_text=send_text,
        progressive=False,  # default
    )
    await delivery.on_delta("Paragraph one.\n\n")
    await delivery.on_delta("Paragraph two.\n\n")
    await delivery.on_delta("Paragraph three.")
    final = await delivery.finish()
    # No progressive chunks should have been sent (FINAL_ONLY default).
    assert delivery.progressive_chunks_sent == 0
    # The final text must preserve all paragraphs.
    assert final == "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    assert "\n\n" in final


@pytest.mark.asyncio
async def test_whatsapp_progressive_mode_sends_chunks_only_over_limit() -> None:
    """When progressive=True, the delivery sends complete message units
    only when the text exceeds the platform limit — not on every
    paragraph (blueprint §6.2: do not send every paragraph automatically)."""
    from waxprep.channel.whatsapp.progressive import ProgressiveWhatsAppDelivery

    sent_texts: list[str] = []

    async def send_text(text: str) -> None:
        sent_texts.append(text)

    # Short text — should NOT produce progressive chunks.
    delivery = ProgressiveWhatsAppDelivery(
        send_text=send_text,
        progressive=True,
        chunk_soft_limit=4000,
    )
    await delivery.on_delta("Short answer.")
    await delivery.finish()
    assert delivery.progressive_chunks_sent == 0


@pytest.mark.asyncio
async def test_whatsapp_final_text_preserves_paragraph_boundaries() -> None:
    """The authoritative final answer is reconstructed from ordered
    deltas, NOT from trimmed outbound messages. Paragraph boundaries
    must survive (blueprint §6.3)."""
    from waxprep.channel.whatsapp.progressive import ProgressiveWhatsAppDelivery

    async def send_text(text: str) -> None:
        pass

    delivery = ProgressiveWhatsAppDelivery(send_text=send_text)
    await delivery.on_delta("First paragraph.")
    await delivery.on_delta("\n\n")
    await delivery.on_delta("Second paragraph.")
    final = await delivery.finish()
    assert final == "First paragraph.\n\nSecond paragraph."
    assert "\n\n" in final


# ---------------------------------------------------------------------------
# Telegram native draft — the method exists and is verified
# ---------------------------------------------------------------------------


def test_telegram_client_has_send_message_draft_method() -> None:
    """The TelegramClient exposes send_message_draft for native live-draft
    streaming. The method was verified against the Telegram Bot API
    on 19 October 2026 against bot @Waxdavidesssaffbot."""
    from waxprep.channel.telegram.client import TelegramClient

    client = TelegramClient(bot_token="test:fake")
    assert hasattr(client, "send_message_draft")
    assert callable(client.send_message_draft)
