"""Student-facing UI: early streaming, typing, marker-safe display,
single-use interactive buttons, and durable delivery on send failure.

Infrastructure-only assertions: pacing/split sizes, retry bookkeeping,
callback acknowledgement, one-shot consume. No educational branching is
tested or implied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import httpx

from support import add_student, make_world
from waxprep.channel.contracts import (
    CONTENT_KIND_INTERACTIVE_BUTTON,
    InteractiveButtonSpec,
    InteractiveSuggestion,
    NormalizedInbound,
    OutboundPayload,
    OutboundResult,
)
from waxprep.channel.streaming.display import ChoicesMarkerFilter
from waxprep.channel.telegram.client import TelegramClient
from waxprep.channel.telegram.handler import (
    TelegramInboundConfig,
    TelegramInboundHandler,
)
from waxprep.channel.telegram.normalize import normalize_telegram_update
from waxprep.channel.telegram.outbox import (
    TelegramOutbox,
    TelegramOutboxConfig,
)
from waxprep.channel.whatsapp.handler import (
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.channel.whatsapp.progressive import ProgressiveWhatsAppDelivery
from waxprep.domain.models import (
    AuthorKind,
    ChannelIdentity,
    HistoryEntry,
    InteractiveOffer,
    InteractiveOfferStatus,
    OutboundDelivery,
    OutboundStatus,
    new_id,
)


@dataclass
class FakeWhatsAppClient:
    """Captures sends and typing calls; can simulate failures."""

    texts: list[str] = field(default_factory=list)
    typing_calls: list[tuple[str, str | None]] = field(default_factory=list)
    fail_sends: bool = False

    async def send_text(self, payload: OutboundPayload) -> OutboundResult:
        if self.fail_sends:
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=True,
                error_code="500",
                error_detail="simulated send failure",
            )
        self.texts.append(payload.text or "")
        return OutboundResult(
            success=True,
            provider_message_id=f"wamid.fake.{len(self.texts)}",
            temporary_failure=False,
        )

    async def send_typing(
        self,
        recipient_external_id: str,
        *,
        message_id: str | None = None,
    ) -> None:
        self.typing_calls.append((recipient_external_id, message_id))


@dataclass
class FakeTelegramClient:
    """Captures sends, drafts, typing, and callback acknowledgements."""

    texts: list[str] = field(default_factory=list)
    keyboards: list[dict | None] = field(default_factory=list)
    typing_calls: list[str] = field(default_factory=list)
    drafts: list[tuple[str, int]] = field(default_factory=list)
    answered: list[tuple[str, str | None]] = field(default_factory=list)
    fail_sends: bool = False
    fail_temporary: bool = True

    async def send_text(
        self,
        payload: OutboundPayload,
        *,
        reply_markup: dict | None = None,
    ) -> OutboundResult:
        if self.fail_sends:
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=self.fail_temporary,
                error_code="429" if self.fail_temporary else "400",
                error_detail="simulated send failure",
            )
        self.texts.append(payload.text or "")
        self.keyboards.append(reply_markup)
        return OutboundResult(
            success=True,
            provider_message_id=f"tg.fake.{len(self.texts)}",
            temporary_failure=False,
        )

    async def send_typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)

    async def send_message_draft(
        self,
        *,
        chat_id: str,
        text: str,
        draft_id: int,
        thread_id: int | None = None,
    ) -> OutboundResult:
        self.drafts.append((text, draft_id))
        return OutboundResult(
            success=True,
            provider_message_id=None,
            temporary_failure=False,
        )

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> None:
        self.answered.append((callback_query_id, text))


def _link(
    world, wax_id, *, channel: str = "whatsapp", external: str = "+2348000000001"
) -> None:
    world.storage.create_channel_identity(
        ChannelIdentity(
            id=new_id(),
            wax_id=wax_id,
            channel=channel,
            external_id=external,
            created_at=world.clock.now(),
        )
    )


def _wa_inbound(wax_id, occurred_at, text, msg_id=None) -> NormalizedInbound:
    return NormalizedInbound(
        channel="whatsapp",
        provider_sender_id="+2348000000001",
        wax_id=wax_id,
        provider_message_id=msg_id or f"wamid.{uuid4().hex}",
        occurred_at=occurred_at,
        text=text,
    )


def _tg_callback(wax_id, occurred_at, button_id, callback_query_id):
    return NormalizedInbound(
        channel="telegram",
        provider_sender_id="100200",
        wax_id=wax_id,
        provider_message_id=callback_query_id,
        occurred_at=occurred_at,
        text=None,
        content_kind=CONTENT_KIND_INTERACTIVE_BUTTON,
        reply_to_provider_id="77",
        structured={
            "button_id": button_id,
            "callback_query_id": callback_query_id,
        },
    )


def _wa_handler(world, client) -> WhatsAppInboundHandler:
    return WhatsAppInboundHandler(
        storage=world.storage,
        history=world.services.history,
        turn_service=world.services.turns,
        clock=world.clock,
        config=WhatsAppInboundConfig(),
        whatsapp_client=client,  # type: ignore[arg-type]
    )


def _tg_handler(world, client, **config_kwargs) -> TelegramInboundHandler:
    return TelegramInboundHandler(
        storage=world.storage,
        history=world.services.history,
        turn_service=world.services.turns,
        clock=world.clock,
        config=TelegramInboundConfig(**config_kwargs),
        telegram_client=client,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Choices marker filter (stream-safe display)
# ---------------------------------------------------------------------------


def test_marker_filter_suppresses_marker_split_across_deltas() -> None:
    f = ChoicesMarkerFilter()
    assert f.feed("Here is the answer.\n\n<<<ch") == "Here is the answer.\n\n"
    assert f.feed("oices>>>\nPractice\nExplain") == ""
    assert f.flush() == ""


def test_marker_filter_releases_held_prefix_on_flush() -> None:
    f = ChoicesMarkerFilter()
    assert f.feed("Partial answer with a less-than sign: ") == (
        "Partial answer with a less-than sign: "
    )
    # "<" and "<<" are proper marker prefixes — held back, not shown.
    assert f.feed("<") == ""
    assert f.feed("<") == ""
    assert f.flush() == "<<"


# ---------------------------------------------------------------------------
# Progressive early flush (WhatsApp streaming)
# ---------------------------------------------------------------------------


async def test_early_flush_sends_first_unit_fast_then_substantial_units():
    sent: list[str] = []

    async def send_text(text: str) -> None:
        sent.append(text)

    delivery = ProgressiveWhatsAppDelivery(
        send_text=send_text,
        progressive=True,
        early_flush_chars=40,
        steady_flush_chars=200,
    )
    sentence = "One two three four five six seven eight nine ten. "
    for word in (sentence * 4).split(" "):
        await delivery.on_delta(word + " ")
    # The first complete sentence was delivered DURING the stream.
    assert any("One two three four five six seven eight nine ten." in t for t in sent)
    # Steady pacing: not every sentence became its own message.
    assert len(sent) < 4
    await delivery.flush_final((sentence * 4).strip())
    joined = "".join(sent)
    # The complete answer was delivered, each sentence exactly once.
    assert joined.count("One two three four five six seven eight nine ten.") == 4


async def test_early_flush_recovers_failed_unit_at_flush():
    sent: list[str] = []
    calls = {"n": 0}

    async def send_text(text: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first chunk send failed")
        sent.append(text)

    delivery = ProgressiveWhatsAppDelivery(
        send_text=send_text,
        progressive=True,
        early_flush_chars=40,
        steady_flush_chars=200,
    )
    for word in "Alpha beta gamma delta. Epsilon zeta eta theta. ".split(" "):
        await delivery.on_delta(word + " ")
    # The first unit failed mid-stream — it was counted, not raised, and
    # must NOT be treated as already-delivered.
    assert delivery.had_failures
    final = "Alpha beta gamma delta. Epsilon zeta eta theta."
    await delivery.flush_final(final)
    # flush_final re-delivers the complete authoritative answer: nothing
    # sent progressively is silently dropped.
    assert "".join(sent) == final


# ---------------------------------------------------------------------------
# WhatsApp handler: typing, split delivery, retryable failures
# ---------------------------------------------------------------------------


async def test_whatsapp_turn_starts_typing_indicator_with_inbound_id():
    world = make_world()
    wax_id = add_student(world)
    _link(world, wax_id)
    client = FakeWhatsAppClient()
    handler = _wa_handler(world, client)
    msg = _wa_inbound(wax_id, world.clock.now(), "Explain gravity simply.")
    result = await handler.handle_message(msg)
    assert result.accepted
    assert client.typing_calls, "typing indicator must be sent at turn start"
    assert client.typing_calls[0] == ("+2348000000001", msg.provider_message_id)


async def test_whatsapp_failed_live_send_leaves_outbound_retryable():
    world = make_world()
    wax_id = add_student(world)
    _link(world, wax_id)
    client = FakeWhatsAppClient(fail_sends=True)
    handler = _wa_handler(world, client)
    result = await handler.handle_message(
        _wa_inbound(wax_id, world.clock.now(), "Explain gravity simply.")
    )
    assert result.accepted
    assert result.student_entry_id is not None
    tutor = world.storage.find_tutor_reply_to(wax_id, result.student_entry_id)
    assert tutor is not None
    outbound = world.storage.get_outbound_delivery_for_tutor(
        wax_id, tutor.id, "whatsapp"
    )
    assert outbound is not None
    # NOT delivered, NOT permanently failed: retryable for the outbox.
    assert outbound.status is OutboundStatus.PENDING
    assert outbound.run_after > world.clock.now()
    assert outbound.last_error == "live_delivery_failed"


async def test_whatsapp_delivery_success_marks_outbound_delivered():
    world = make_world()
    wax_id = add_student(world)
    _link(world, wax_id)
    client = FakeWhatsAppClient()
    handler = _wa_handler(world, client)
    result = await handler.handle_message(
        _wa_inbound(wax_id, world.clock.now(), "Explain gravity simply.")
    )
    assert result.accepted
    tutor = world.storage.find_tutor_reply_to(wax_id, result.student_entry_id)
    assert tutor is not None
    outbound = world.storage.get_outbound_delivery_for_tutor(
        wax_id, tutor.id, "whatsapp"
    )
    assert outbound is not None
    assert outbound.status is OutboundStatus.DELIVERED
    # Every live piece respects the platform transport limit.
    assert client.texts
    assert all(len(t) <= 4000 for t in client.texts)


# ---------------------------------------------------------------------------
# Telegram: callback normalize, drafts, webhook, buttons, outbox
# ---------------------------------------------------------------------------


def test_telegram_normalize_extracts_callback_query() -> None:
    payload = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-17",
            "from": {"id": 42, "is_bot": False, "username": "ada"},
            "message": {
                "message_id": 77,
                "date": 1730000000,
                "chat": {"id": 100200, "type": "private"},
                "text": "Choose a next step:",
            },
            "data": "opaque-button-id",
        },
    }
    msgs = normalize_telegram_update(payload)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.channel == "telegram"
    assert m.provider_sender_id == "100200"
    assert m.provider_message_id == "cb-17"
    assert m.content_kind == CONTENT_KIND_INTERACTIVE_BUTTON
    assert m.reply_to_provider_id == "77"
    assert m.structured["button_id"] == "opaque-button-id"
    assert m.structured["callback_query_id"] == "cb-17"
    assert m.text is None


async def test_telegram_draft_sink_accumulates_with_integer_draft_id():
    world = make_world()
    client = FakeTelegramClient()
    handler = _tg_handler(world, client, draft_min_interval_seconds=0.0)
    sink = handler._create_draft_sink(
        client,  # type: ignore[arg-type]
        "100200",
        draft_id=uuid4().int & ((1 << 62) - 1) | 1,
        min_interval_seconds=0.0,
    )
    await sink("Tel")
    await sink("egram accumulates ")
    await sink("the full text. ")
    assert len(client.drafts) == 3
    texts = [t for t, _ in client.drafts]
    assert texts[0] == "Tel"
    assert texts[2] == "Telegram accumulates the full text. "
    # draft_id: non-zero integer, reused for updates to the same draft.
    ids = {i for _, i in client.drafts}
    assert len(ids) == 1
    draft_id = ids.pop()
    assert isinstance(draft_id, int)
    assert draft_id != 0
    # The choices trailer never leaks into the preview, even when the
    # marker is split across deltas.
    await sink("\n\n<<<ch")
    await sink("oices>>>\nPractice\nExplain")
    assert all("<<<choices>>>" not in t for t, _ in client.drafts)
    assert all("<<<" not in t for t, _ in client.drafts)


async def test_telegram_set_webhook_requests_callback_updates():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient(bot_token="t:test", http_client=http)
        await client.set_webhook("https://example.com/hook", secret_token="s3cret")
    assert captured["url"].endswith("/setWebhook")
    # callback_query updates MUST be requested or button clicks never arrive.
    assert captured["body"]["allowed_updates"] == ["message", "callback_query"]
    assert captured["body"]["secret_token"] == "s3cret"
    await client.aclose()


async def test_telegram_button_click_runs_turn_then_second_click_rejected():
    world = make_world()
    wax_id = add_student(world)
    _link(world, wax_id, channel="telegram", external="100200")
    client = FakeTelegramClient()
    handler = _tg_handler(world, client, draft_min_interval_seconds=0.0)
    now = world.clock.now()

    button_id = "opaque-b1"
    world.storage.create_interactive_offer(
        InteractiveOffer(
            id=new_id(),
            wax_id=wax_id,
            tutor_entry_id=None,
            provider_outbound_message_id="tg.99",
            buttons=({"id": button_id, "title": "Practice question"},),
            status=InteractiveOfferStatus.OPEN,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=48),
        )
    )

    result = await handler.handle_message(_tg_callback(wax_id, now, button_id, "cbq-1"))
    assert result.accepted
    assert result.error is None
    # The click became an ordinary student turn carrying the button title.
    assert result.student_entry_id is not None
    entry = world.storage.get_history_entry(wax_id, result.student_entry_id)
    assert entry is not None
    assert entry.text == "Practice question"
    tutor = world.storage.find_tutor_reply_to(wax_id, result.student_entry_id)
    assert tutor is not None
    outbound = world.storage.get_outbound_delivery_for_tutor(
        wax_id, tutor.id, "telegram"
    )
    assert outbound is not None
    assert outbound.status is OutboundStatus.DELIVERED
    # The callback was acknowledged (no client spinner) and the answer
    # was delivered as a complete message.
    assert ("cbq-1", None) in client.answered
    assert client.texts

    # Second click on the now-consumed offer: politely rejected, no turn.
    result2 = await handler.handle_message(
        _tg_callback(wax_id, now, button_id, "cbq-2")
    )
    assert result2.accepted
    assert result2.error == "interactive_offer_consumed"
    assert result2.student_entry_id is None
    assert ("cbq-2", "This option was already used.") in client.answered

    # Unknown/stale button: rejected without a turn.
    result3 = await handler.handle_message(
        _tg_callback(wax_id, now, "no-such-button", "cbq-3")
    )
    assert result3.error == "interactive_offer_unavailable"
    assert ("cbq-3", "This option is no longer available.") in client.answered


async def test_telegram_issue_interactive_sends_inline_keyboard_and_offer():
    world = make_world()
    wax_id = add_student(world)
    _link(world, wax_id, channel="telegram", external="100200")
    client = FakeTelegramClient()
    handler = _tg_handler(world, client)

    suggestion = InteractiveSuggestion(
        body_text="Choose a next step:",
        buttons=(
            InteractiveButtonSpec(title="Practice question"),
            InteractiveButtonSpec(title="Explain simpler"),
        ),
    )
    await handler._issue_interactive(
        wax_id=wax_id,
        recipient="100200",
        suggestion=suggestion,
        tutor_entry_id=None,
        outbound_delivery_id=None,
        body_delivered=True,
    )
    assert len(client.texts) == 1
    kb = client.keyboards[0]
    assert kb is not None
    rows = kb["inline_keyboard"]
    # One button per row, as rendered.
    assert len(rows) == 2
    assert [len(r) for r in rows] == [1, 1]
    assert rows[0][0]["text"] == "Practice question"
    # callback_data is an opaque id (not the title), within Telegram's
    # 64-byte limit.
    data = rows[0][0]["callback_data"]
    assert data != "Practice question"
    assert 1 <= len(data) <= 64
    # The one-shot offer was registered under the same opaque id.
    offer = world.storage.find_interactive_offer_by_button_id(wax_id, data)
    assert offer is not None


async def test_telegram_outbox_temporary_failure_is_retried():
    world = make_world()
    wax_id = add_student(world)
    _link(world, wax_id, channel="telegram", external="100200")
    client = FakeTelegramClient(fail_sends=True)
    outbox = TelegramOutbox(
        storage=world.storage,
        client=client,  # type: ignore[arg-type]
        clock=world.clock,
        config=TelegramOutboxConfig(),
    )
    now = world.clock.now()
    tutor_entry = HistoryEntry(
        id=new_id(),
        wax_id=wax_id,
        author_kind=AuthorKind.TUTOR,
        content_kind="text/plain",
        text="The stored authoritative reply.",
        occurred_at=now,
        recorded_at=now,
        channel="telegram",
        channel_ref=None,
    )
    world.storage.append_history_entry(tutor_entry)
    delivery = world.storage.create_outbound_delivery(
        OutboundDelivery(
            id=new_id(),
            wax_id=wax_id,
            tutor_entry_id=tutor_entry.id,
            channel="telegram",
            channel_ref=None,
            provider_message_id=None,
            status=OutboundStatus.PENDING,
            attempts=0,
            last_error=None,
            delivered_at=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )

    await outbox._deliver(delivery)
    updated = world.storage.get_outbound_delivery_for_tutor(
        wax_id, tutor_entry.id, "telegram"
    )
    assert updated is not None
    # Temporary failure: retry scheduled, not permanently dropped.
    assert updated.status is OutboundStatus.PENDING
    assert updated.run_after > now
    assert updated.attempts == 1

    # Recovery on the next attempt: same stored reply, now delivered.
    client.fail_sends = False
    await outbox._deliver(updated)
    final = world.storage.get_outbound_delivery_for_tutor(
        wax_id, tutor_entry.id, "telegram"
    )
    assert final is not None
    assert final.status is OutboundStatus.DELIVERED
    assert "The stored authoritative reply." in "".join(client.texts)


async def test_telegram_outbox_permanent_failure_is_marked_failed():
    world = make_world()
    wax_id = add_student(world)
    _link(world, wax_id, channel="telegram", external="100200")
    client = FakeTelegramClient(fail_sends=True, fail_temporary=False)
    outbox = TelegramOutbox(
        storage=world.storage,
        client=client,  # type: ignore[arg-type]
        clock=world.clock,
        config=TelegramOutboxConfig(),
    )
    now = world.clock.now()
    tutor_entry = HistoryEntry(
        id=new_id(),
        wax_id=wax_id,
        author_kind=AuthorKind.TUTOR,
        content_kind="text/plain",
        text="A reply that will be rejected.",
        occurred_at=now,
        recorded_at=now,
        channel="telegram",
        channel_ref=None,
    )
    world.storage.append_history_entry(tutor_entry)
    delivery = world.storage.create_outbound_delivery(
        OutboundDelivery(
            id=new_id(),
            wax_id=wax_id,
            tutor_entry_id=tutor_entry.id,
            channel="telegram",
            channel_ref=None,
            provider_message_id=None,
            status=OutboundStatus.PENDING,
            attempts=0,
            last_error=None,
            delivered_at=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )

    await outbox._deliver(delivery)
    updated = world.storage.get_outbound_delivery_for_tutor(
        wax_id, tutor_entry.id, "telegram"
    )
    assert updated is not None
    assert updated.status is OutboundStatus.FAILED
