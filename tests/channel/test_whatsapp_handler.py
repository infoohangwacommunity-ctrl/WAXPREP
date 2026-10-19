"""Tests for WhatsApp inbound handler."""

from datetime import UTC, datetime

import pytest

from support import TEST_CIPHER
from waxprep.channel.contracts import NormalizedInbound
from waxprep.channel.whatsapp.handler import (
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.clock import SystemClock
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    ChannelIdentity,
    Student,
    new_id,
)
from waxprep.history.service import HistoryService
from waxprep.index.indexer import Indexer
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst
from waxprep.storage.memory import InMemoryStorage


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def clock() -> SystemClock:
    return SystemClock()


@pytest.fixture
def indexer(storage, clock) -> Indexer:
    from waxprep.index.tokens import TokenIndex

    return Indexer(storage, TokenIndex(TEST_CIPHER), clock)


@pytest.fixture
def history(storage, indexer, clock) -> HistoryService:
    return HistoryService(storage, indexer, clock)


@pytest.fixture
def handler(storage, history, clock) -> WhatsAppInboundHandler:
    # Create a mock turn service
    from unittest.mock import AsyncMock

    turn_service = AsyncMock(spec=TurnServiceCiFirst)

    return WhatsAppInboundHandler(
        storage=storage,
        history=history,
        turn_service=turn_service,
        clock=clock,
        config=WhatsAppInboundConfig(),
    )


class TestSenderResolution:
    """Test sender resolution via ChannelIdentity lookup."""

    def test_resolve_known_sender(self, storage, handler) -> None:
        """Test resolving a known WhatsApp number to WAX ID."""
        wax_id = new_wax_id()
        student = Student(
            wax_id=wax_id,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        storage.create_student(student)

        identity = ChannelIdentity(
            id=new_id(),
            wax_id=wax_id,
            channel="whatsapp",
            external_id="2348012345678",
            created_at=datetime.now(tz=UTC),
        )
        storage.create_channel_identity(identity)

        result = handler.resolve_sender("2348012345678")
        assert result == wax_id

    def test_resolve_unknown_sender(self, storage, handler) -> None:
        """Test resolving an unknown WhatsApp number returns None."""
        result = handler.resolve_sender("2348012345678")
        assert result is None


class TestMessageHandling:
    """Test inbound message handling."""

    @pytest.mark.asyncio
    async def test_handle_message_unlinked_sender(self, handler) -> None:
        """Test handling message from unlinked sender."""
        msg = NormalizedInbound(
            channel="whatsapp",
            provider_sender_id="2348012345678",
            wax_id=None,
            provider_message_id="wamid.TEST123",
            occurred_at=datetime.now(tz=UTC),
            text="Hello, can you help me?",
            attachments=(),
            metadata={},
        )

        result = await handler.handle_message(msg)

        assert result.accepted is True
        assert result.wax_id is None
        assert result.needs_linking is True
        assert result.student_entry_id is None

    @pytest.mark.asyncio
    async def test_handle_message_linked_sender(
        self, storage, history, handler
    ) -> None:
        """Test handling message from linked sender."""
        wax_id = new_wax_id()
        student = Student(
            wax_id=wax_id,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        storage.create_student(student)

        identity = ChannelIdentity(
            id=new_id(),
            wax_id=wax_id,
            channel="whatsapp",
            external_id="2348012345678",
            created_at=datetime.now(tz=UTC),
        )
        storage.create_channel_identity(identity)

        msg = NormalizedInbound(
            channel="whatsapp",
            provider_sender_id="2348012345678",
            wax_id=wax_id,
            provider_message_id="wamid.TEST123",
            occurred_at=datetime.now(tz=UTC),
            text="Hello, can you help me?",
            attachments=(),
            metadata={},
        )

        result = await handler.handle_message(msg)

        assert result.accepted is True
        assert result.wax_id == wax_id
        assert result.needs_linking is False
        # Student entry should be created
        assert result.student_entry_id is not None

    @pytest.mark.asyncio
    async def test_handle_duplicate_message(self, storage, history, handler) -> None:
        """Test handling duplicate message (idempotency)."""
        wax_id = new_wax_id()
        student = Student(
            wax_id=wax_id,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        storage.create_student(student)

        identity = ChannelIdentity(
            id=new_id(),
            wax_id=wax_id,
            channel="whatsapp",
            external_id="2348012345678",
            created_at=datetime.now(tz=UTC),
        )
        storage.create_channel_identity(identity)

        msg = NormalizedInbound(
            channel="whatsapp",
            provider_sender_id="2348012345678",
            wax_id=wax_id,
            provider_message_id="wamid.TEST123",
            occurred_at=datetime.now(tz=UTC),
            text="Hello, can you help me?",
            attachments=(),
            metadata={},
        )

        # First delivery
        result1 = await handler.handle_message(msg)
        assert result1.accepted is True
        assert result1.student_entry_id is not None

        # Duplicate delivery
        result2 = await handler.handle_message(msg)
        assert result2.accepted is True
        assert result2.student_entry_id == result1.student_entry_id
        assert result2.accepted is True  # idempotent re-delivery


class TestLinkingResponse:
    """Test linking instruction responses."""

    def test_create_linking_response(self, handler) -> None:
        """Test creating a linking response for unlinked senders."""
        response = handler.create_linking_response("2348012345678")
        assert "link" in response.lower() and "whatsapp" in response.lower()

    def test_create_disabled_response(self, handler) -> None:
        """Test creating a disabled account response."""
        response = handler.create_disabled_response()
        assert "disabled" in response.lower()
