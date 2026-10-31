"""Founder fix (formatting): one canonical answer, channel-native renderers.

The Teacher produces markdown-like text. The renderer transforms it
into channel-compatible syntax WITHOUT paraphrasing, removing warnings,
or changing calculations. Blueprint §7.

These tests verify the rendering fix is in place for both channels
and that the outbound delivery path actually invokes the renderer
before sending — no raw markdown reaches WhatsApp or Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest

from support import TEST_CIPHER
from waxprep.channel.contracts import NormalizedInbound, OutboundPayload
from waxprep.channel.streaming.renderers import (
    render_plain,
    render_telegram,
    render_whatsapp,
)
from waxprep.channel.whatsapp.handler import (
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.channel.whatsapp.outbox import OutboxConfig, WhatsAppOutbox
from waxprep.clock import FakeClock
from waxprep.context.evolution import EvolutionService
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.models import InvestigationLimits
from waxprep.context.retrieval import Retriever
from waxprep.context.tool_impls import build_default_tools
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    ChannelIdentity,
    HistoryEntry,
    OutboundDelivery,
    OutboundStatus,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.orchestration.contracts_ci_first import Teacher as TeacherProtocol
from waxprep.orchestration.mock_teacher import MockTeacher, MockTeacherScript
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst
from waxprep.storage.memory import InMemoryStorage

# ---------------------------------------------------------------------------
# WhatsApp: **bold** → *bold* (single-asterisk)
# ---------------------------------------------------------------------------


def test_whatsapp_bold_renders_to_single_asterisk() -> None:
    """WhatsApp uses single-asterisk for bold, not Markdown's double-asterisk.
    A Teacher reply of ``**important**`` must reach the student as
    ``*important*`` so WhatsApp actually renders it bold.
    """
    result = render_whatsapp("**bold**")
    assert result.text == "*bold*", (
        f"expected '*bold*' (WhatsApp syntax), got {result.text!r}"
    )
    assert result.format_mode == "whatsapp"
    assert not result.fallback_used


def test_whatsapp_bold_inside_sentence_renders_correctly() -> None:
    """Bold can appear mid-sentence, not just as a standalone token."""
    result = render_whatsapp("Please review **this section** carefully.")
    assert result.text == "Please review *this section* carefully."
    assert "*this section*" in result.text
    assert "**" not in result.text


def test_whatsapp_italic_renders_to_underscore() -> None:
    """Markdown ``*italic*`` → WhatsApp ``_italic_``."""
    result = render_whatsapp("*italic text*")
    assert result.text == "_italic text_"


def test_whatsapp_code_block_renders_to_triple_backticks() -> None:
    """WhatsApp uses triple-backticks for code blocks."""
    result = render_whatsapp("```python\nprint('hi')\n```")
    # The language tag is stripped (WhatsApp ignores it).
    assert "```" in result.text
    assert "print('hi')" in result.text


def test_whatsapp_empty_input_returns_empty() -> None:
    result = render_whatsapp("")
    assert result.text == ""
    assert result.format_mode == "whatsapp"


# ---------------------------------------------------------------------------
# Telegram: **bold** → <b>bold</b>
# ---------------------------------------------------------------------------


def test_telegram_bold_renders_to_html_b_tag() -> None:
    """Telegram HTML parse_mode uses <b> for bold (or <strong>).
    Markdown's ``**bold**`` must become ``<b>bold</b>`` so Telegram
    renders it bold.
    """
    result = render_telegram("**bold**")
    assert result.text in ("<b>bold</b>", "<strong>bold</strong>"), (
        f"expected '<b>bold</b>' (Telegram HTML), got {result.text!r}"
    )
    assert result.format_mode == "HTML"
    assert not result.fallback_used


def test_telegram_bold_inside_sentence_renders_correctly() -> None:
    result = render_telegram("Please review **this section** carefully.")
    assert "<b>this section</b>" in result.text
    assert "**" not in result.text


def test_telegram_italic_renders_to_html_i_tag() -> None:
    result = render_telegram("*italic text*")
    assert result.text == "<i>italic text</i>"


def test_telegram_html_special_characters_are_escaped() -> None:
    """``&``, ``<``, ``>`` outside tags must be escaped so Telegram's
    HTML parser does not choke (blueprint §7.5).
    """
    result = render_telegram("a < b & c > d")
    assert "&lt;" in result.text
    assert "&amp;" in result.text
    assert "&gt;" in result.text


# ---------------------------------------------------------------------------
# Plain-text fallback
# ---------------------------------------------------------------------------


def test_plain_renderer_strips_all_formatting() -> None:
    """The fallback renderer strips formatting markers but preserves
    text content. Used when rich formatting fails (blueprint §10.3).
    """
    result = render_plain("**bold** and _italic_ and `code`")
    assert result.text == "bold and italic and code"
    assert result.format_mode == "plain"


# ---------------------------------------------------------------------------
# Outbound delivery path: the renderer IS invoked before send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whatsapp_outbox_renders_before_send() -> None:
    """The WhatsApp outbox's ``_attempt_delivery`` must invoke
    ``render_whatsapp`` on the tutor reply text BEFORE splitting and
    sending. A mock client captures the rendered text; we assert the
    text the platform received has WhatsApp syntax, not Markdown.
    """
    now = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    storage = InMemoryStorage()
    clock = FakeClock(now)
    wax = new_wax_id()
    storage.create_student(
        Student(wax_id=wax, status=StudentStatus.ACTIVE, created_at=now, updated_at=now)
    )
    storage.create_channel_identity(
        ChannelIdentity(
            id=new_id(), wax_id=wax, channel="whatsapp", external_id="1555PHONE",
            created_at=now,
        )
    )
    # Save a tutor reply containing markdown bold.
    tutor_text = "Welcome! **Important:** review fractions first."
    tutor_entry, _ = storage.append_history_entry_with_jobs(
        HistoryEntry(
            id=new_id(), wax_id=wax, author_kind=AuthorKind.TUTOR,
            content_kind="text/plain", text=tutor_text,
            occurred_at=now, recorded_at=now,
            channel="whatsapp", channel_ref=None, reply_to_id=new_id(),
        ),
        token_hashes=(),
        jobs=(),
    )
    delivery = storage.create_outbound_delivery(
        OutboundDelivery(
            id=new_id(), wax_id=wax, tutor_entry_id=tutor_entry.id,
            channel="whatsapp", channel_ref=None, provider_message_id=None,
            status=OutboundStatus.PENDING, attempts=0, last_error=None,
            delivered_at=None, run_after=now, created_at=now, updated_at=now,
        )
    )

    # Mock the WhatsApp client to capture the text the platform sees.
    captured_texts: list[str] = []

    async def _send_text(payload: OutboundPayload):
        captured_texts.append(payload.text)
        return type("R", (), {"success": True, "provider_message_id": "wamid_X"})()

    client = AsyncMock()
    client.send_text = _send_text  # type: ignore[assignment]
    client.send_typing = None
    outbox = WhatsAppOutbox(storage, client, clock, config=OutboxConfig(batch_size=10))  # type: ignore[arg-type]
    await outbox._process_delivery(delivery, now)

    assert captured_texts, "outbox did not invoke send_text"
    # The text the platform received MUST be WhatsApp-rendered: bold is *x*, not **x**.
    sent = captured_texts[0]
    assert "*Important:*" in sent, (
        f"outbox did not render markdown to WhatsApp syntax; sent={sent!r}"
    )
    assert "**" not in sent, "raw markdown ** reached the platform"


@pytest.mark.asyncio
async def test_whatsapp_handler_final_send_renders_before_send() -> None:
    """The WhatsApp inbound handler's ``_send_final_text`` path (used
    when progressive delivery is off or no chunks were sent) must
    invoke ``render_whatsapp`` before calling the client. A mock
    client captures the rendered text.
    """
    now = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    storage = InMemoryStorage()
    clock = FakeClock(now)
    token_index = TokenIndex(TEST_CIPHER)
    indexer = Indexer(storage, token_index, clock, has_embedder=False)
    history = HistoryService(storage, indexer, clock)
    retriever = Retriever(storage, token_index)
    evolution = EvolutionService(storage=storage, indexer=indexer)
    investigator = ContextInvestigator(
        model=MockContextModel(),
        tools=build_default_tools(storage, retriever, None, evolution=evolution),
    )
    teacher = MockTeacher(
        script=MockTeacherScript(
            instructions=[("reply_without_ci", "Hi! **Bold greeting** for you.")]
        )
    )
    turns = TurnServiceCiFirst(
        storage, history, investigator, cast(TeacherProtocol, teacher), clock,
        ci_limits=InvestigationLimits(max_steps=2, max_tool_calls=2),
    )

    wax = new_wax_id()
    storage.create_student(
        Student(wax_id=wax, status=StudentStatus.ACTIVE, created_at=now, updated_at=now)
    )
    storage.create_channel_identity(
        ChannelIdentity(
            id=new_id(), wax_id=wax, channel="whatsapp", external_id="1555PHONE2",
            created_at=now,
        )
    )

    captured: list[str] = []

    async def _send_text(payload: OutboundPayload):
        captured.append(payload.text)
        return type("R", (), {"success": True, "provider_message_id": "wamid_Y"})()

    client = AsyncMock()
    client.send_text = _send_text  # type: ignore[assignment]
    client.send_typing = AsyncMock(return_value=None)

    handler = WhatsAppInboundHandler(
        storage,
        history,
        turns,
        clock,
        whatsapp_client=client,  # type: ignore[arg-type]
        config=WhatsAppInboundConfig(),  # type: ignore[arg-type]
    )
    msg = NormalizedInbound(
        channel="whatsapp", wax_id=None,
        provider_message_id="wamid.FMT_TEST",
        provider_sender_id="1555PHONE2",
        text="hi", occurred_at=now, content_kind="text",
        attachments=(), structured={}, reply_to_provider_id=None,
    )
    result = await handler.handle_message(msg)
    assert result.accepted
    assert captured, "handler did not send any text to the client"
    # Every captured text must use WhatsApp syntax, never raw ** markdown.
    for text in captured:
        assert "**" not in text, (
            f"raw markdown reached WhatsApp: {text!r}"
        )
        # The bold greeting should appear in WhatsApp single-asterisk form.
        if "Bold greeting" in text:
            assert "*Bold greeting*" in text
