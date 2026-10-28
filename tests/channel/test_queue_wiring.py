"""End-to-end wiring: burst queue behind the WhatsApp handler and the
native Telegram handler with draft streaming.

Proves the live wiring contract: every burst message is saved verbatim
exactly once, one coalesced tutor turn runs per burst (time-window
grouping only — texts here are content-free), and the native Telegram
handler streams deltas and always sends the authoritative final text.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from support import add_student, make_world
from waxprep.channel.contracts import (
    NormalizedInbound,
    OutboundPayload,
    OutboundResult,
)
from waxprep.channel.message_queue import (
    MessageQueue,
    QueueConfig,
    QueuedMessage,
)
from waxprep.channel.streaming.contract import ChannelCapability
from waxprep.channel.streaming.renderers import telegram_capabilities
from waxprep.channel.telegram.client import TelegramClient
from waxprep.channel.telegram.handler import (
    TelegramInboundConfig,
    TelegramInboundHandler,
)
from waxprep.channel.whatsapp.handler import (
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import AuthorKind, ChannelIdentity, new_id
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst


def _wa_msg(i: int) -> NormalizedInbound:
    return NormalizedInbound(
        channel="whatsapp",
        wax_id=None,
        provider_message_id=f"wamid.WIRE-{i}",
        provider_sender_id="15551234567",
        text=f"part {i}",
        occurred_at=datetime(2026, 10, 27, 11, 0, tzinfo=UTC),
        content_kind="text",
        attachments=(),
        structured={},
        reply_to_provider_id=None,
    )


def _tg_msg() -> NormalizedInbound:
    return NormalizedInbound(
        channel="telegram",
        wax_id=None,
        provider_message_id="tg.WIRE-1",
        provider_sender_id="777000",
        text="hello tutor",
        occurred_at=datetime(2026, 10, 27, 11, 0, tzinfo=UTC),
        content_kind="text",
        attachments=(),
        structured={},
        reply_to_provider_id=None,
    )


class RecordingTelegramClient(TelegramClient):
    """Test double that records sends; no network is ever touched.

    EVERY method the handler can reach is overridden. The real client
    lazily creates an httpx.AsyncClient on first use; a missed override
    would open a REAL socket (e.g. sendChatAction on turn start) that
    nobody closes, and its leaked transport resurfaces later as an
    unraisable ResourceWarning inside an unrelated test.
    """

    def __init__(self) -> None:
        super().__init__("test-token")
        self.sent_texts: list[str] = []
        self.drafts: list[str] = []
        self.typing_calls: list[str] = []
        self.callback_answers: list[tuple[str, str | None]] = []

    async def send_text(self, payload: OutboundPayload) -> OutboundResult:
        self.sent_texts.append(payload.text or "")
        return OutboundResult(
            success=True,
            provider_message_id=f"tg-{len(self.sent_texts)}",
            temporary_failure=False,
        )

    async def send_message_draft(
        self,
        *,
        chat_id: str,
        text: str,
        draft_id: str,
        thread_id: int | None = None,
    ) -> OutboundResult:
        self.drafts.append(text)
        return OutboundResult(
            success=True,
            provider_message_id=f"draft-{len(self.drafts)}",
            temporary_failure=False,
        )

    async def send_typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> None:
        self.callback_answers.append((callback_query_id, text))


@pytest.mark.asyncio
async def test_whatsapp_burst_one_turn_all_messages_saved() -> None:
    world = make_world()
    wax = add_student(world)
    now = world.clock.now()
    world.storage.create_channel_identity(
        ChannelIdentity(
            id=new_id(),
            wax_id=wax,
            channel="whatsapp",
            external_id="15551234567",
            created_at=now,
        )
    )
    turns: TurnServiceCiFirst = world.services.turns  # type: ignore[assignment]

    # Mirror the app composition: the runner dispatches to the singleton
    # handler registered per channel, exactly like app.py's queue_handlers.
    handlers: dict[str, WhatsAppInboundHandler] = {}

    async def run_turn(wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        await handlers["whatsapp"].run_queued_turn(wax_id, batch)

    queue = MessageQueue(
        world.clock,
        QueueConfig(coalesce_window_seconds=0.05, worker_idle_timeout_seconds=2.0),
        turn_runner=run_turn,
    )
    handlers["whatsapp"] = WhatsAppInboundHandler(
        world.storage,
        world.services.history,
        turns,
        world.clock,
        config=WhatsAppInboundConfig(channel="whatsapp"),
        message_queue=queue,
    )
    wa_handler = handlers["whatsapp"]

    results = [await wa_handler.handle_message(_wa_msg(i)) for i in range(3)]
    assert all(r.accepted for r in results)
    assert all(r.student_entry_id is not None for r in results)
    assert all(r.outbound_delivery_id is None for r in results)

    deadline = time.monotonic() + 5.0
    while len(world.teacher.seen_inputs) < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    await queue.stop()

    assert len(world.teacher.seen_inputs) == 1
    entries = world.storage.list_history(wax, limit=50)
    students = [e for e in entries if e.author_kind is AuthorKind.STUDENT]
    tutors = [e for e in entries if e.author_kind is AuthorKind.TUTOR]
    assert len(students) == 3
    assert len(tutors) == 1
    assert queue.get_queue_length() == 0


@pytest.mark.asyncio
async def test_telegram_native_handler_streams_and_sends_final() -> None:
    world = make_world()
    wax = add_student(world)
    now = world.clock.now()
    world.storage.create_channel_identity(
        ChannelIdentity(
            id=new_id(),
            wax_id=wax,
            channel="telegram",
            external_id="777000",
            created_at=now,
        )
    )
    turns: TurnServiceCiFirst = world.services.turns  # type: ignore[assignment]

    tg_client = RecordingTelegramClient()
    tg_handler = TelegramInboundHandler(
        world.storage,
        world.services.history,
        turns,
        world.clock,
        config=TelegramInboundConfig(),
        telegram_client=tg_client,
    )

    result = await tg_handler.handle_message(_tg_msg())
    assert result.accepted
    # A swallowed turn failure must fail the test loudly instead of
    # silently leaving the tutor reply missing.
    assert result.error is None, f"telegram turn failed: {result.error}"
    assert result.student_entry_id is not None

    # The turn starts with a best-effort typing indicator — recorded,
    # never a real sendChatAction request.
    assert tg_client.typing_calls == ["777000"]

    entries = world.storage.list_history(wax, limit=50)
    tutors = [e for e in entries if e.author_kind is AuthorKind.TUTOR]
    assert len(tutors) == 1
    assert len(world.teacher.seen_inputs) == 1

    # The final complete message always carries the authoritative reply.
    assert tg_client.sent_texts
    assert tg_client.sent_texts[-1] == "Let's work on it together."
    # When the channel advertises native drafts, deltas stream as drafts
    # before the final message lands.
    if telegram_capabilities().has(ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES):
        assert tg_client.drafts
