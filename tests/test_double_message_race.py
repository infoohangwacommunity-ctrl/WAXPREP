"""Regression: the double-message race between live progressive delivery
and the durable outbox worker.

Race (handler.py:602-633, pre-fix)
----------------------------------
The Teacher streams chunks to the student via ``on_text_delta`` during
``handle_student_message``. After that returns, the outbound delivery row
exists with ``status='pending'``. The outbox worker (same event loop,
separate DB connection) may claim and send the full text — on top of the
chunks the student already received.

Fix (Option A — atomic delivery flag)
-------------------------------------
The handler calls the atomic ``mark_outbound_delivered`` immediately after
``handle_student_message`` returns. The CAS guard ``delivered_at IS NULL``
makes this idempotent with the outbox's own ``mark_outbound_delivered``:
whichever path wins the CAS commits the delivery, the other is a no-op.
``mark_outbound_failed`` clears ``delivered_at`` on retry so the row stays
internally consistent (status=pending AND delivered_at IS NULL) and the
outbox's later ``mark_outbound_delivered`` (CAS) can succeed.

Why Option A (not B)
--------------------
Option B (claim before stream) requires the ``outbound_delivery_id``
BEFORE the Teacher streams. The row is created inside
``TurnServiceCiFirst.handle_student_message`` (after the tutor reply is
saved), so Option B would require restructuring the turn service — out of
scope for this fix and invasive to many tests. Option A is additive:
one atomic method, one early call site, no signature change to the turn
service.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest

from support import TEST_CIPHER
from waxprep.channel.contracts import NormalizedInbound, OutboundResult
from waxprep.channel.whatsapp.handler import WhatsAppInboundHandler
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
# Shared test stack — mirrors tests/channel/test_whatsapp_idempotency.py but
# wires a mock WhatsApp client so progressive delivery is exercised end-to-end.
# ---------------------------------------------------------------------------


def _stack(clock: FakeClock):
    storage = InMemoryStorage()
    cipher = TEST_CIPHER
    token_index = TokenIndex(cipher)
    indexer = Indexer(storage, token_index, clock, has_embedder=False)
    history = HistoryService(storage, indexer, clock)
    retriever = Retriever(storage, token_index)
    evolution = EvolutionService(storage=storage, indexer=indexer)
    investigator = ContextInvestigator(
        model=MockContextModel(),
        tools=build_default_tools(storage, retriever, None, evolution=evolution),
    )
    teacher = MockTeacher(
        script=MockTeacherScript(instructions=[("reply", "Hello from Wax Prep.")])
    )
    turns = TurnServiceCiFirst(
        storage,
        history,
        investigator,
        cast(TeacherProtocol, teacher),
        clock,
        ci_limits=InvestigationLimits(max_steps=2, max_tool_calls=2),
    )
    return storage, history, turns


def _seed_student(
    storage: InMemoryStorage, clock: FakeClock, phone: str = "15551234567"
):
    wax = new_wax_id()
    now = clock.now()
    storage.create_student(
        Student(
            wax_id=wax,
            status=StudentStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    storage.create_channel_identity(
        ChannelIdentity(
            id=new_id(),
            wax_id=wax,
            channel="whatsapp",
            external_id=phone,
            created_at=now,
        )
    )
    return wax


def _msg(
    wamid: str, text: str = "hello", phone: str = "15551234567"
) -> NormalizedInbound:
    return NormalizedInbound(
        channel="whatsapp",
        wax_id=None,
        provider_message_id=wamid,
        provider_sender_id=phone,
        text=text,
        occurred_at=datetime(2026, 10, 22, 14, 0, tzinfo=UTC),
        content_kind="text",
        attachments=(),
        structured={},
        reply_to_provider_id=None,
    )


def _ok_result(msg_id: str = "wamid.OUTBOUND_OK") -> OutboundResult:
    return OutboundResult(
        success=True,
        provider_message_id=msg_id,
        temporary_failure=False,
    )


def _get_delivery(storage: InMemoryStorage, wax, delivery_id) -> OutboundDelivery:
    """Direct dict access — same pattern as tests/test_outbound_recovery.py."""
    return storage._outbound[(wax, delivery_id)]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test 1: progressive delivery completing marks the outbound delivered
# atomically; a subsequent outbox claim returns None.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progressive_delivery_marks_outbound_delivered() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, turns = _stack(clock)
    wax = _seed_student(storage, clock)

    sent_texts: list[str] = []

    async def _send_text(payload) -> OutboundResult:
        sent_texts.append(payload.text or "")
        return _ok_result()

    client = AsyncMock()
    client.send_text = _send_text
    client.send_typing = AsyncMock(return_value=None)

    handler = WhatsAppInboundHandler(
        storage, history, turns, clock, whatsapp_client=client
    )
    msg = _msg("wamid.RACE_OK_1", "hi")
    result = await handler.handle_message(msg)

    assert result.accepted
    assert result.outbound_delivery_id is not None

    delivery = _get_delivery(storage, wax, result.outbound_delivery_id)
    assert delivery.status is OutboundStatus.DELIVERED, (
        "progressive delivery completing must mark the outbound delivered"
    )
    assert delivery.delivered_at is not None, (
        "delivered_at must be set so the outbox's claim_outbound_delivery "
        "(WHERE status='pending') returns None"
    )

    # The outbox worker must NOT be able to claim a delivered row.
    reclaimed = storage.claim_outbound_delivery(
        wax, result.outbound_delivery_id, clock.now()
    )
    assert reclaimed is None, (
        "outbox claim on a delivered row must return None — the row is "
        "already delivered and must not be re-sent"
    )

    # Sanity: at least one chunk was sent to the student via progressive
    # delivery (the live path IS the delivery).
    assert sent_texts, "progressive delivery must have sent at least one chunk"


# ---------------------------------------------------------------------------
# Test 2: progressive delivery raising must NOT leave the row marked
# delivered. The outbox worker must still be able to claim and retry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progressive_failure_does_not_mark() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, turns = _stack(clock)
    wax = _seed_student(storage, clock)

    send_count = 0

    async def _send_text_failing(payload) -> OutboundResult:
        nonlocal send_count
        send_count += 1
        raise RuntimeError("simulated network failure")

    client = AsyncMock()
    client.send_text = _send_text_failing
    client.send_typing = AsyncMock(return_value=None)

    handler = WhatsAppInboundHandler(
        storage, history, turns, clock, whatsapp_client=client
    )
    msg = _msg("wamid.RACE_FAIL_1", "hi")
    result = await handler.handle_message(msg)

    assert result.accepted
    assert result.outbound_delivery_id is not None

    delivery = _get_delivery(storage, wax, result.outbound_delivery_id)
    assert delivery.delivered_at is None, (
        "progressive delivery failure must NOT leave the row marked "
        "delivered — mark_outbound_failed must clear delivered_at"
    )
    assert delivery.status is OutboundStatus.PENDING, (
        "a failed live send must revert the row to pending so the outbox "
        "retries; got " + str(delivery.status)
    )

    # The outbox worker must still be able to claim and retry.
    reclaimed = storage.claim_outbound_delivery(
        wax, result.outbound_delivery_id, clock.now()
    )
    assert reclaimed is not None, (
        "outbox claim on a reverted row must succeed — the row is pending "
        "and the outbox must recover it"
    )
    assert reclaimed.status is OutboundStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Test 3: the outbox worker skips an already-delivered row. Seed a
# delivery with delivered_at set and assert claim returns None.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_worker_skips_already_delivered() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, _turns = _stack(clock)
    wax = _seed_student(storage, clock)
    student, _ = history.record_with_status(
        wax, AuthorKind.STUDENT, "hi", channel="whatsapp", channel_ref="wamid.SKIP"
    )
    tutor, _ = history.record_with_status(
        wax, AuthorKind.TUTOR, "answer", channel="whatsapp", reply_to_id=student.id
    )
    now = clock.now()
    delivery = storage.create_outbound_delivery(
        OutboundDelivery(
            id=new_id(),
            wax_id=wax,
            tutor_entry_id=tutor.id,
            channel="whatsapp",
            channel_ref=None,
            status=OutboundStatus.DELIVERED,
            attempts=1,
            last_error=None,
            delivered_at=now,
            run_after=now,
            created_at=now,
            updated_at=now,
            provider_message_id="wamid.ALREADY_SENT",
        )
    )

    # claim_outbound_delivery uses WHERE status='pending' — a delivered row
    # cannot be claimed.
    reclaimed = storage.claim_outbound_delivery(wax, delivery.id, now)
    assert reclaimed is None, (
        "outbox claim on a delivered row must return None — no re-send"
    )

    # The atomic mark_outbound_delivered must also be a no-op (CAS on
    # delivered_at IS NULL fails). This is the idempotency guarantee: a
    # late live-handler mark after the outbox already delivered is safe.
    marked = storage.mark_outbound_delivered(wax, delivery.id, now)
    assert marked is False, (
        "mark_outbound_delivered on an already-delivered row must return "
        "False — the CAS guard makes it idempotent"
    )

    # The outbox worker's _process_delivery also short-circuits on
    # status=DELIVERED before even trying to claim.
    client = AsyncMock()
    client.send_text = AsyncMock(return_value=_ok_result())
    outbox = WhatsAppOutbox(
        storage,
        client,
        clock,
        config=OutboxConfig(batch_size=10, poll_interval_seconds=60),
    )
    await outbox._process_delivery(delivery, now)
    client.send_text.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: full race simulation. The live progressive-delivery path and the
# outbox worker both attempt to deliver the same outbound row. Only one
# send may reach the student.
#
# The race is resolved at the storage layer by the atomic CAS guard
# ``delivered_at IS NULL`` in mark_outbound_delivered: whichever path
# wins the CAS commits the delivery, the other's claim/mark is a no-op.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_race_simulation() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, _turns = _stack(clock)
    wax = _seed_student(storage, clock)
    student, _ = history.record_with_status(
        wax, AuthorKind.STUDENT, "hi", channel="whatsapp", channel_ref="wamid.FULL_RACE"
    )
    tutor, _ = history.record_with_status(
        wax,
        AuthorKind.TUTOR,
        "the complete tutor reply",
        channel="whatsapp",
        reply_to_id=student.id,
    )
    now = clock.now()
    delivery = storage.create_outbound_delivery(
        OutboundDelivery(
            id=new_id(),
            wax_id=wax,
            tutor_entry_id=tutor.id,
            channel="whatsapp",
            channel_ref=None,
            status=OutboundStatus.PENDING,
            attempts=0,
            last_error=None,
            delivered_at=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )

    # Both the live path and the outbox will try to send. The shared
    # send_text counter proves only one path actually sent.
    sends: list[str] = []

    async def _live_send(payload) -> OutboundResult:
        # The live progressive path: mark delivered atomically FIRST
        # (this is the fix), then send.
        storage.mark_outbound_delivered(wax, delivery.id, clock.now())
        sends.append("live:" + (payload.text or ""))
        return _ok_result("wamid.LIVE")

    async def _outbox_send(payload) -> OutboundResult:
        sends.append("outbox:" + (payload.text or ""))
        return _ok_result("wamid.OUTBOX")

    # Simulate the two paths racing via asyncio.gather. The live path
    # runs the handler's atomic-mark-first flow; the outbox path runs
    # the outbox's claim-then-send flow. The CAS guard ensures only one
    # send reaches the student.
    async def live_path() -> None:
        # Mirror the handler's early atomic mark: mark delivered BEFORE
        # sending, so the outbox's claim returns None.
        storage.mark_outbound_delivered(wax, delivery.id, clock.now())
        # Yield so the outbox path gets a chance to race (it will lose).
        await asyncio.sleep(0)
        # Send the progressive chunk (simulated).
        from waxprep.channel.contracts import OutboundPayload

        await _live_send(
            OutboundPayload(
                channel="whatsapp",
                wax_id=wax,
                recipient_external_id="15551234567",
                text="the complete tutor reply",
            )
        )

    outbox_client = AsyncMock()
    outbox_client.send_text = _outbox_send
    outbox_client.send_typing = AsyncMock(return_value=None)
    outbox = WhatsAppOutbox(
        storage,
        outbox_client,
        clock,
        config=OutboxConfig(batch_size=10, poll_interval_seconds=60),
    )

    async def outbox_path() -> None:
        # Yield so the live path gets a chance to mark first.
        await asyncio.sleep(0)
        await outbox._process_delivery(delivery, clock.now())

    await asyncio.gather(live_path(), outbox_path())

    # Only the live path must have sent. The outbox's claim must have
    # returned None (status=delivered) so its send_text was never called.
    live_sends = [s for s in sends if s.startswith("live:")]
    outbox_sends = [s for s in sends if s.startswith("outbox:")]
    assert len(live_sends) == 1, (
        f"the live path must send exactly once; got {live_sends}"
    )
    assert len(outbox_sends) == 0, (
        "the outbox must NOT send — the live path won the atomic mark "
        "and the outbox's claim returned None; got " + str(outbox_sends)
    )

    # The row must be delivered (not pending, not in_progress).
    final = _get_delivery(storage, wax, delivery.id)
    assert final.status is OutboundStatus.DELIVERED
    assert final.delivered_at is not None
