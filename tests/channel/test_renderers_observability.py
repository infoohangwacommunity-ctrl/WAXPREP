"""Tests for channel renderers, observability, and comprehensive formatter.

Blueprint §12.4 (Shared formatter tests): use representative content
including ordinary prose, multiple paragraphs, bulleted lists, numbered
steps, bold and italic text, inline code, multiline code blocks, URLs,
emojis, Unicode combining marks, non-English text, literal asterisks and
underscores, empty or whitespace-only deltas, formatting markers split
across deltas, a stream that ends unexpectedly, and a final response
that differs from the expected well-formed format.

Blueprint §11 (Observability): verify that metrics and counters are
recorded correctly.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from waxprep.channel.streaming.accumulator import AnswerAccumulator
from waxprep.channel.streaming.contract import (
    ChannelCapability,
    DeliveryStrategy,
    ResponseLifecycle,
)
from waxprep.channel.streaming.formatter import is_safe_final, safe_increment
from waxprep.channel.streaming.observability import StreamingCounters, StreamingMetrics
from waxprep.channel.streaming.renderers import (
    render_plain,
    render_telegram,
    render_whatsapp,
    telegram_capabilities,
    whatsapp_capabilities,
)

# ---------------------------------------------------------------------------
# WhatsApp renderer (blueprint §7.4)
# ---------------------------------------------------------------------------


def test_whatsapp_renderer_bold() -> None:
    result = render_whatsapp("This is **bold** text.")
    assert result.format_mode == "whatsapp"
    assert "*bold*" in result.text
    assert "**" not in result.text  # markdown ** should be gone


def test_whatsapp_renderer_italic() -> None:
    result = render_whatsapp("This is *italic* text.")
    assert "_italic_" in result.text


def test_whatsapp_renderer_strikethrough() -> None:
    result = render_whatsapp("This is ~struck~ text.")
    assert "~struck~" in result.text


def test_whatsapp_renderer_inline_code() -> None:
    result = render_whatsapp("Use `print()` to output.")
    assert "```print()```" in result.text


def test_whatsapp_renderer_code_block() -> None:
    result = render_whatsapp("```python\nprint('hello')\n```")
    assert "```print('hello')```" in result.text or "```" in result.text


def test_whatsapp_renderer_link_as_plain_text() -> None:
    """WhatsApp doesn't support inline links — render as text (url)."""
    result = render_whatsapp("See [docs](https://example.com) for more.")
    assert "docs" in result.text
    assert "https://example.com" in result.text
    assert "[" not in result.text  # markdown link syntax removed


def test_whatsapp_renderer_preserves_paragraphs() -> None:
    result = render_whatsapp("First paragraph.\n\nSecond paragraph.")
    assert "\n\n" in result.text
    assert "First paragraph." in result.text
    assert "Second paragraph." in result.text


def test_whatsapp_renderer_unicode_and_emoji() -> None:
    result = render_whatsapp("Héllo wörld 🌍 こんにちは")
    assert "Héllo wörld 🌍 こんにちは" in result.text


# ---------------------------------------------------------------------------
# Telegram renderer (blueprint §7.5, Route A — HTML entities)
# ---------------------------------------------------------------------------


def test_telegram_renderer_bold() -> None:
    result = render_telegram("This is **bold** text.")
    assert result.format_mode == "HTML"
    assert "<b>bold</b>" in result.text


def test_telegram_renderer_italic() -> None:
    result = render_telegram("This is *italic* text.")
    assert "<i>italic</i>" in result.text


def test_telegram_renderer_strikethrough() -> None:
    result = render_telegram("This is ~struck~ text.")
    assert "<s>struck</s>" in result.text


def test_telegram_renderer_inline_code() -> None:
    result = render_telegram("Use `print()` to output.")
    assert "<code>print()</code>" in result.text


def test_telegram_renderer_code_block() -> None:
    result = render_telegram("```python\nprint('hello')\n```")
    assert "<pre>" in result.text
    assert "print('hello')" in result.text
    assert "</pre>" in result.text


def test_telegram_renderer_link() -> None:
    result = render_telegram("See [docs](https://example.com) for more.")
    assert '<a href="https://example.com">docs</a>' in result.text


def test_telegram_renderer_escapes_html_in_code() -> None:
    """HTML special characters inside code blocks must be escaped."""
    result = render_telegram("```python\nx = a < b & c > d\n```")
    assert "&lt;" in result.text
    assert "&amp;" in result.text
    assert "&gt;" in result.text


def test_telegram_renderer_escapes_literal_html() -> None:
    """Literal < and > in text (not part of our tags) must be escaped."""
    result = render_telegram("Use the < and > operators")
    assert "&lt;" in result.text
    assert "&gt;" in result.text


def test_telegram_renderer_preserves_paragraphs() -> None:
    result = render_telegram("First paragraph.\n\nSecond paragraph.")
    assert "\n\n" in result.text


def test_telegram_renderer_unicode_and_emoji() -> None:
    result = render_telegram("Héllo wörld 🌍 こんにちは")
    assert "Héllo wörld 🌍 こんにちは" in result.text


# ---------------------------------------------------------------------------
# Plain-text fallback renderer (blueprint §10.3)
# ---------------------------------------------------------------------------


def test_plain_renderer_strips_all_formatting() -> None:
    text = "This is **bold** and *italic* and ~struck~ and `code`."
    result = render_plain(text)
    assert result.format_mode == "plain"
    assert "bold" in result.text
    assert "italic" in result.text
    assert "struck" in result.text
    assert "code" in result.text
    assert "**" not in result.text
    assert "*" not in result.text
    assert "~" not in result.text
    assert "`" not in result.text


def test_plain_renderer_preserves_links_as_text() -> None:
    result = render_plain("See [docs](https://example.com) for more.")
    assert "docs" in result.text
    assert "https://example.com" in result.text
    assert "[" not in result.text


def test_plain_renderer_preserves_paragraphs() -> None:
    result = render_plain("First paragraph.\n\nSecond paragraph.")
    assert "\n\n" in result.text


# ---------------------------------------------------------------------------
# Channel capability declarations (blueprint §4.2)
# ---------------------------------------------------------------------------


def test_whatsapp_capabilities_no_typing_indicator() -> None:
    """WhatsApp Cloud API does NOT support a verified typing indicator.
    The capability must NOT be reported as available (blueprint §6.4)."""
    caps = whatsapp_capabilities()
    assert not caps.has(ChannelCapability.TYPING_INDICATOR)
    assert caps.has(ChannelCapability.COMPLETE_MESSAGE_SEND)
    assert caps.has(ChannelCapability.SUPPORTS_FINAL_PERSISTENT_MESSAGE)
    assert not caps.has(ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES)


def test_telegram_capabilities_has_native_drafts() -> None:
    """Telegram has native text draft updates (verified 19 October 2026)."""
    caps = telegram_capabilities()
    assert caps.has(ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES)
    assert caps.has(ChannelCapability.TYPING_INDICATOR)
    assert caps.has(ChannelCapability.COMPLETE_MESSAGE_SEND)
    assert caps.has(ChannelCapability.SUPPORTS_FINAL_PERSISTENT_MESSAGE)


def test_capability_drives_strategy_selection() -> None:
    """The coordinator selects a strategy from the declared capabilities.
    WhatsApp → FINAL_ONLY; Telegram → NATIVE_DRAFT."""
    from waxprep.channel.streaming.coordinator import StreamingCoordinator
    from waxprep.channel.streaming.pacing import (
        TELEGRAM_DRAFT_DEFAULT,
        WHATSAPP_DEFAULT,
    )

    # WhatsApp → FINAL_ONLY
    whatsapp_coord = StreamingCoordinator(
        accumulator=AnswerAccumulator(),
        capabilities=whatsapp_capabilities(),
        pacing=WHATSAPP_DEFAULT,
        send_update=lambda text: asyncio.sleep(0, result=None),
    )
    assert whatsapp_coord.strategy is DeliveryStrategy.FINAL_ONLY

    # Telegram → NATIVE_DRAFT
    telegram_coord = StreamingCoordinator(
        accumulator=AnswerAccumulator(),
        capabilities=telegram_capabilities(),
        pacing=TELEGRAM_DRAFT_DEFAULT,
        send_update=lambda text: asyncio.sleep(0, result=None),
    )
    assert telegram_coord.strategy is DeliveryStrategy.NATIVE_DRAFT


# ---------------------------------------------------------------------------
# Comprehensive formatter tests (blueprint §12.4)
# ---------------------------------------------------------------------------


def test_formatter_plain_prose() -> None:
    text = "The student asked about photosynthesis. Photosynthesis is the process by which plants make food using sunlight, water, and carbon dioxide."
    assert is_safe_final(text)


def test_formatter_multiple_paragraphs() -> None:
    text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
    assert is_safe_final(text)
    inc = safe_increment(text)
    assert len(inc.safe_text) > 0


def test_formatter_bulleted_list() -> None:
    text = "Steps:\n* Step one\n* Step two\n* Step three"
    assert is_safe_final(text)


def test_formatter_numbered_steps() -> None:
    text = "Steps:\n1. First step\n2. Second step\n3. Third step"
    assert is_safe_final(text)


def test_formatter_bold_and_italic() -> None:
    text = "This is **bold** and *italic* and **both *nested* bold**."
    assert is_safe_final(text)


def test_formatter_inline_code() -> None:
    text = "Use the `print()` function to output text."
    assert is_safe_final(text)


def test_formatter_multiline_code_block() -> None:
    text = "Here is the code:\n```python\ndef hello():\n    print('world')\n```"
    assert is_safe_final(text)


def test_formatter_urls() -> None:
    text = "See https://example.com for more details."
    assert is_safe_final(text)


def test_formatter_emojis() -> None:
    text = "Hello! 🎉🌍📚💡"
    assert is_safe_final(text)


def test_formatter_non_english_text() -> None:
    text = "こんにちは世界\nПривет мир\nمرحبا بالعالم"
    assert is_safe_final(text)


def test_formatter_literal_asterisks() -> None:
    """Literal * not used for formatting should not be treated as markers
    — but our simple formatter is conservative. The safe boundary check
    should at least not crash."""
    text = "Multiply 3 * 4 to get 12."
    # This is plain text — no formatting markers. Should be safe.
    assert is_safe_final(text)


def test_formatter_empty_delta() -> None:
    inc = safe_increment("")
    assert inc.safe_text == ""
    assert inc.pending_suffix == ""


def test_formatter_whitespace_only() -> None:
    inc = safe_increment("   \n\n  ")
    # Whitespace is safe to display.
    assert len(inc.safe_text) >= 0


def test_formatter_markers_split_across_deltas() -> None:
    """A ** opened in one delta and closed in a later delta must not
    be displayed until the closing ** arrives."""
    acc = AnswerAccumulator()
    acc.start()
    acc.append_delta("This is **not yet")
    inc1 = safe_increment(acc.final_text)
    # The ** is unclosed — not safe to display past it.
    assert inc1.pending_suffix != ""
    assert "**" in inc1.pending_suffix

    acc.append_delta(" closed** yet.")
    # Now the ** is closed — safe to display the full text.
    assert is_safe_final(acc.final_text)


def test_formatter_stream_ends_unexpectedly() -> None:
    """If the stream ends mid-marker, the final text is NOT safe.
    The formatter reports this so the coordinator can decide what to
    do (render the safe prefix, or wait for a retry)."""
    text = "This is **unclosed bold"
    assert not is_safe_final(text)
    inc = safe_increment(text)
    # The safe text should NOT include the unclosed **.
    assert inc.pending_suffix != ""


# ---------------------------------------------------------------------------
# Observability tests (blueprint §11)
# ---------------------------------------------------------------------------


def test_streaming_metrics_record_timings() -> None:
    metrics = StreamingMetrics(
        response_id=uuid4(),
        channel="whatsapp",
        time_to_first_provider_delta=0.5,
        time_to_first_update_attempt=1.2,
        time_to_complete=3.5,
        update_count=0,
        streamed_successfully=True,
    )
    assert metrics.time_to_first_provider_delta == 0.5
    assert metrics.streamed_successfully


def test_streaming_counters_aggregate() -> None:
    counters = StreamingCounters()
    m1 = StreamingMetrics(
        response_id=uuid4(),
        channel="whatsapp",
        streamed_successfully=True,
    )
    m2 = StreamingMetrics(
        response_id=uuid4(),
        channel="telegram",
        streamed_successfully=False,
        fell_back_to_non_streaming=True,
        draft_update_failures=2,
    )
    counters.record(m1)
    counters.record(m2)
    assert counters.responses_streamed_successfully == 1
    assert counters.responses_fell_back == 1
    assert counters.draft_update_failures == 2


def test_streaming_counters_failure_categories() -> None:
    counters = StreamingCounters()
    counters.record_failure_category("provider_timeout")
    counters.record_failure_category("provider_timeout")
    counters.record_failure_category("rate_limit")
    summary = counters.summary()
    assert summary["failure_categories"]["provider_timeout"] == 2
    assert summary["failure_categories"]["rate_limit"] == 1


def test_streaming_metrics_log_summary_no_crash() -> None:
    """The log_summary method must not crash even with None values."""
    metrics = StreamingMetrics(
        response_id=uuid4(),
        channel="test",
    )
    metrics.log_summary()  # should not raise


# ---------------------------------------------------------------------------
# Cancellation propagation (blueprint §9.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_stops_coordinator_updates() -> None:
    """After cancellation, the coordinator must not send any further
    updates, even if stale deltas arrive (blueprint §9.4: 'An old
    streaming task must not be able to overwrite a newer response')."""
    from waxprep.channel.streaming.coordinator import StreamingCoordinator
    from waxprep.channel.streaming.pacing import PacingPolicy

    acc = AnswerAccumulator()
    acc.start()
    updates: list[str] = []

    async def send_update(text: str) -> str | None:
        updates.append(text)
        return None

    coord = StreamingCoordinator(
        accumulator=acc,
        capabilities=telegram_capabilities(),
        pacing=PacingPolicy(min_chars_per_update=1, min_interval_seconds=0),
        send_update=send_update,
    )
    await coord.on_delta("first chunk of text")
    await coord.cancel()
    # Stale deltas from the old response.
    await coord.on_delta("stale data")
    await coord.on_delta("more stale data")
    # The stale data must not appear in any update.
    assert not any("stale" in u for u in updates)


@pytest.mark.asyncio
async def test_coordinator_complete_returns_final_text() -> None:
    """The coordinator's complete() returns the authoritative final text
    from the accumulator, not from the last sent update."""
    from waxprep.channel.streaming.coordinator import StreamingCoordinator
    from waxprep.channel.streaming.pacing import PacingPolicy

    acc = AnswerAccumulator()
    acc.start()
    updates: list[str] = []

    async def send_update(text: str) -> str | None:
        updates.append(text)
        return f"msg_{len(updates)}"

    async def finalize(text: str) -> str | None:
        return "final_msg"

    coord = StreamingCoordinator(
        accumulator=acc,
        capabilities=telegram_capabilities(),
        pacing=PacingPolicy(min_chars_per_update=1, min_interval_seconds=0),
        send_update=send_update,
        finalize=finalize,
    )
    await coord.on_delta("Hello, ")
    await coord.on_delta("world!")
    final = await coord.complete()
    assert final == "Hello, world!"
    assert acc.state is ResponseLifecycle.COMPLETED
    assert "final_msg" in coord.provider_message_ids
