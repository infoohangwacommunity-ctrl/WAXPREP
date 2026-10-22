"""Regression: duplicate WhatsApp webhook / concurrent turn / job retry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from support import TEST_CIPHER
from waxprep.channel.contracts import NormalizedInbound
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
    DerivationJob,
    JobStatus,
    ObjectType,
    OutboundDelivery,
    OutboundStatus,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.orchestration.mock_teacher import MockTeacher, MockTeacherScript
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst
from waxprep.storage.memory import InMemoryStorage


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
        teacher,
        clock,
        ci_limits=InvestigationLimits(max_steps=2, max_tool_calls=2),
    )
    return storage, history, turns


def _seed_student(storage: InMemoryStorage, clock: FakeClock, phone: str = "15551234567"):
    wax = new_wax_id()
    now = clock.now()
    storage.create_student(
        Student(wax_id=wax, status=StudentStatus.ACTIVE, created_at=now, updated_at=now)
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


def _msg(wamid: str, text: str = "hello", phone: str = "15551234567") -> NormalizedInbound:
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


@pytest.mark.asyncio
async def test_duplicate_webhook_same_wamid_single_tutor_reply() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, turns = _stack(clock)
    wax = _seed_student(storage, clock)
    handler = WhatsAppInboundHandler(storage, history, turns, clock)
    msg = _msg("wamid.DUPLICATE_TEST_1")
    r1 = await handler.handle_message(msg)
    r2 = await handler.handle_message(msg)
    assert r1.accepted and r2.accepted
    assert r1.student_entry_id == r2.student_entry_id
    tutors = [e for e in storage.list_history(wax, limit=50) if e.author_kind is AuthorKind.TUTOR]
    assert len(tutors) == 1


@pytest.mark.asyncio
async def test_concurrent_handle_same_message_one_tutor() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, turns = _stack(clock)
    wax = _seed_student(storage, clock)
    handler = WhatsAppInboundHandler(storage, history, turns, clock)
    msg = _msg("wamid.CONCURRENT_1", "hi concurrent")
    results = await asyncio.gather(
        handler.handle_message(msg),
        handler.handle_message(msg),
        handler.handle_message(msg),
    )
    assert all(r.accepted for r in results)
    tutors = [e for e in storage.list_history(wax, limit=50) if e.author_kind is AuthorKind.TUTOR]
    assert len(tutors) == 1


@pytest.mark.asyncio
async def test_process_turn_job_after_tutor_exists_is_noop() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, turns = _stack(clock)
    wax = _seed_student(storage, clock)
    student, _ = history.record_with_status(
        wax,
        AuthorKind.STUDENT,
        "need recovery",
        channel="whatsapp",
        channel_ref="wamid.RECOVERY_1",
    )
    result = await turns.handle_student_message(
        wax,
        "need recovery",
        channel="whatsapp",
        channel_ref="wamid.RECOVERY_1",
        occurred_at=student.occurred_at,
    )
    assert result.tutor_entry is not None
    job = DerivationJob(
        id=new_id(),
        wax_id=wax,
        kind="process_turn",
        object_type=ObjectType.HISTORY,
        object_id=student.id,
        status=JobStatus.PENDING,
        attempts=1,
        run_after=clock.now(),
        created_at=clock.now(),
        updated_at=clock.now(),
        last_error="simulated",
    )
    recovered = await turns.process_turn_job(job)
    assert recovered.duplicate is True
    tutors = [e for e in storage.list_history(wax, limit=50) if e.author_kind is AuthorKind.TUTOR]
    assert len(tutors) == 1


@pytest.mark.asyncio
async def test_tutor_record_race_returns_existing_not_raise() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, _turns = _stack(clock)
    wax = _seed_student(storage, clock)
    student, _ = history.record_with_status(
        wax, AuthorKind.STUDENT, "x", channel="whatsapp", channel_ref="wamid.RACE"
    )
    t1, c1 = history.record_with_status(
        wax, AuthorKind.TUTOR, "first answer", channel="whatsapp", reply_to_id=student.id
    )
    t2, c2 = history.record_with_status(
        wax,
        AuthorKind.TUTOR,
        "second answer should not replace",
        channel="whatsapp",
        reply_to_id=student.id,
    )
    assert c1 is True
    assert c2 is False
    assert t1.id == t2.id
    assert t2.text == "first answer"


@pytest.mark.asyncio
async def test_outbox_skips_already_delivered() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, _turns = _stack(clock)
    wax = _seed_student(storage, clock)
    student, _ = history.record_with_status(
        wax, AuthorKind.STUDENT, "hi", channel="whatsapp", channel_ref="wamid.OUT"
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
            provider_message_id="wamid.OUTBOUND_SENT",
        )
    )
    client = AsyncMock()
    client.send_text = AsyncMock(
        return_value=type("R", (), {"success": True, "provider_message_id": "x"})()
    )
    outbox = WhatsAppOutbox(
        storage, client, clock, config=OutboxConfig(batch_size=10, poll_interval_seconds=60)
    )
    await outbox._process_delivery(delivery, now)
    client.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_failure_after_reply_process_turn_does_not_second_tutor() -> None:
    clock = FakeClock(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    storage, history, turns = _stack(clock)
    wax = _seed_student(storage, clock)
    r1 = await turns.handle_student_message(
        wax, "q1", channel="whatsapp", channel_ref="wamid.POST_FAIL"
    )
    assert r1.tutor_entry is not None
    r2 = await turns.handle_student_message(
        wax, "q1", channel="whatsapp", channel_ref="wamid.POST_FAIL"
    )
    assert r2.duplicate is True
    tutors = [e for e in storage.list_history(wax, limit=50) if e.author_kind is AuthorKind.TUTOR]
    assert len(tutors) == 1
