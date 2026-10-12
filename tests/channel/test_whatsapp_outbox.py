"""Tests for WhatsApp outbox delivery service."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from waxprep.channel.contracts import OutboundPayload, OutboundResult
from waxprep.channel.whatsapp.client import WhatsAppClient
from waxprep.channel.whatsapp.outbox import WhatsAppOutbox, OutboxConfig
from waxprep.clock import SystemClock
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    ChannelIdentity,
    HistoryEntry,
    OutboundDelivery,
    OutboundStatus,
    Student,
    new_id,
)
from waxprep.storage.memory import InMemoryStorage


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def client() -> WhatsAppClient:
    return WhatsAppClient(
        token="test_token",
        phone_number_id="test_phone_id",
    )


@pytest.fixture
def clock() -> SystemClock:
    return SystemClock()


@pytest.fixture
def outbox(storage, client, clock) -> WhatsAppOutbox:
    return WhatsAppOutbox(
        storage=storage,
        client=client,
        clock=clock,
        config=OutboxConfig(),
    )


class TestDeliveryEnqueue:
    """Test enqueuing deliveries."""

    def test_enqueue_delivery(self, storage, outbox) -> None:
        """Test enqueuing a new outbound delivery."""
        wax_id = new_wax_id()
        student = Student(wax_id=wax_id, created_at=datetime.now(tz=timezone.utc), updated_at=datetime.now(tz=timezone.utc))
        storage.create_student(student)
        
        tutor_entry = HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.TUTOR,
            content_kind="text/plain",
            text="This is a test response",
            occurred_at=datetime.now(tz=timezone.utc),
            recorded_at=datetime.now(tz=timezone.utc),
            channel="whatsapp",
            channel_ref="test_ref",
        )
        storage.append_history_entry(tutor_entry)
        
        delivery = asyncio.run(outbox.enqueue_delivery(
            wax_id, tutor_entry, "test_ref"
        ))
        
        assert delivery.id is not None
        assert delivery.wax_id == wax_id
        assert delivery.tutor_entry_id == tutor_entry.id
        assert delivery.channel == "whatsapp"
        assert delivery.status == OutboundStatus.PENDING


class TestDeliveryProcessing:
    """Test delivery processing."""

    @pytest.mark.asyncio
    async def test_process_delivery_success(self, storage, outbox) -> None:
        """Test successful delivery processing."""
        wax_id = new_wax_id()
        student = Student(wax_id=wax_id, created_at=datetime.now(tz=timezone.utc), updated_at=datetime.now(tz=timezone.utc))
        storage.create_student(student)
        
        identity = ChannelIdentity(
            id=new_id(),
            wax_id=wax_id,
            channel="whatsapp",
            external_id="2348012345678",
            created_at=datetime.now(tz=timezone.utc),
        )
        storage.create_channel_identity(identity)
        
        tutor_entry = HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.TUTOR,
            content_kind="text/plain",
            text="This is a test response",
            occurred_at=datetime.now(tz=timezone.utc),
            recorded_at=datetime.now(tz=timezone.utc),
            channel="whatsapp",
            channel_ref="test_ref",
        )
        storage.append_history_entry(tutor_entry)
        
        delivery = await outbox.enqueue_delivery(wax_id, tutor_entry, "test_ref")
        
        # Mock successful delivery
        outbox._client.send_text = AsyncMock(return_value=OutboundResult(
            success=True,
            provider_message_id="wamid.SUCCESS123",
            temporary_failure=False,
        ))
        
        now = datetime.now(tz=timezone.utc)
        await outbox._process_delivery(delivery, now)
        
        # Check delivery was marked as delivered
        updated = storage.get_outbound_delivery_for_tutor(
            wax_id, tutor_entry.id, "whatsapp"
        )
        assert updated is not None
        assert updated.status == OutboundStatus.DELIVERED

    @pytest.mark.asyncio
    async def test_process_delivery_temporary_failure(self, storage, outbox) -> None:
        """Test delivery with temporary failure and retry."""
        wax_id = new_wax_id()
        student = Student(wax_id=wax_id, created_at=datetime.now(tz=timezone.utc), updated_at=datetime.now(tz=timezone.utc))
        storage.create_student(student)
        
        identity = ChannelIdentity(
            id=new_id(),
            wax_id=wax_id,
            channel="whatsapp",
            external_id="2348012345678",
            created_at=datetime.now(tz=timezone.utc),
        )
        storage.create_channel_identity(identity)
        
        tutor_entry = HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.TUTOR,
            content_kind="text/plain",
            text="This is a test response",
            occurred_at=datetime.now(tz=timezone.utc),
            recorded_at=datetime.now(tz=timezone.utc),
            channel="whatsapp",
            channel_ref="test_ref",
        )
        storage.append_history_entry(tutor_entry)
        
        delivery = await outbox.enqueue_delivery(wax_id, tutor_entry, "test_ref")
        
        # Mock temporary failure
        outbox._client.send_text = AsyncMock(return_value=OutboundResult(
            success=False,
            provider_message_id=None,
            temporary_failure=True,
            error_code="timeout",
            error_detail="request timed out",
        ))
        
        now = datetime.now(tz=timezone.utc)
        await outbox._process_delivery(delivery, now)
        
        # Check delivery was marked as failed with retry
        updated = storage.get_outbound_delivery_for_tutor(
            wax_id, tutor_entry.id, "whatsapp"
        )
        assert updated is not None
        assert updated.status == OutboundStatus.FAILED
        assert updated.attempts == 1
        assert updated.last_error == "request timed out"
        assert updated.run_after > now


class TestBackoffCalculation:
    """Test exponential backoff calculation."""

    def test_calculate_backoff(self, outbox) -> None:
        """Test backoff calculation."""
        # First attempt
        assert outbox._calculate_backoff(1) == 1.0
        
        # Second attempt
        assert outbox._calculate_backoff(2) == 2.0
        
        # Third attempt
        assert outbox._calculate_backoff(3) == 4.0
        
        # Fourth attempt
        assert outbox._calculate_backoff(4) == 8.0


class TestDeliveryStatus:
    """Test delivery status callback handling."""

    @pytest.mark.asyncio
    async def test_handle_delivery_status_delivered(self, storage, outbox) -> None:
        """Test handling delivered status callback."""
        wax_id = new_wax_id()
        student = Student(wax_id=wax_id, created_at=datetime.now(tz=timezone.utc), updated_at=datetime.now(tz=timezone.utc))
        storage.create_student(student)
        
        tutor_entry = HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.TUTOR,
            content_kind="text/plain",
            text="This is a test response",
            occurred_at=datetime.now(tz=timezone.utc),
            recorded_at=datetime.now(tz=timezone.utc),
            channel="whatsapp",
            channel_ref="wamid.TEST123",
        )
        storage.append_history_entry(tutor_entry)
        
        delivery = OutboundDelivery(
            id=new_id(),
            wax_id=wax_id,
            tutor_entry_id=tutor_entry.id,
            channel="whatsapp",
            channel_ref="wamid.TEST123",
            status=OutboundStatus.PENDING,
            attempts=0,
            last_error=None,
            delivered_at=None,
            run_after=datetime.now(tz=timezone.utc),
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
        storage.create_outbound_delivery(delivery)
        
        result = await outbox.handle_delivery_status(
            wax_id, "wamid.TEST123", "delivered"
        )
        
        assert result is True
        
        updated = storage.get_outbound_delivery_for_tutor(
            wax_id, tutor_entry.id, "whatsapp"
        )
        assert updated is not None
        assert updated.status == OutboundStatus.DELIVERED

    @pytest.mark.asyncio
    async def test_handle_delivery_status_failed(self, storage, outbox) -> None:
        """Test handling failed status callback."""
        wax_id = new_wax_id()
        student = Student(wax_id=wax_id, created_at=datetime.now(tz=timezone.utc), updated_at=datetime.now(tz=timezone.utc))
        storage.create_student(student)
        
        tutor_entry = HistoryEntry(
            id=new_id(),
            wax_id=wax_id,
            author_kind=AuthorKind.TUTOR,
            content_kind="text/plain",
            text="This is a test response",
            occurred_at=datetime.now(tz=timezone.utc),
            recorded_at=datetime.now(tz=timezone.utc),
            channel="whatsapp",
            channel_ref="wamid.TEST123",
        )
        storage.append_history_entry(tutor_entry)
        
        delivery = OutboundDelivery(
            id=new_id(),
            wax_id=wax_id,
            tutor_entry_id=tutor_entry.id,
            channel="whatsapp",
            channel_ref="wamid.TEST123",
            status=OutboundStatus.PENDING,
            attempts=0,
            last_error=None,
            delivered_at=None,
            run_after=datetime.now(tz=timezone.utc),
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
        storage.create_outbound_delivery(delivery)
        
        result = await outbox.handle_delivery_status(
            wax_id, "wamid.TEST123", "failed"
        )
        
        assert result is True
        
        updated = storage.get_outbound_delivery_for_tutor(
            wax_id, tutor_entry.id, "whatsapp"
        )
        assert updated is not None
        assert updated.status == OutboundStatus.FAILED
