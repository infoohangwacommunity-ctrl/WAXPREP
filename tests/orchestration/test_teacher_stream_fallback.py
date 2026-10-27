"""Teacher streaming fallbacks: empty stream and mid-stream failure.

A stream can complete without any usable text delta, or die halfway
through. Both cases must fall back to one complete gateway call and
still produce an authoritative tutor reply — never silence, never a
partial answer presented as final.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from support import add_student, make_model_teacher_world
from waxprep.model_gateway.errors import ModelGatewayError
from waxprep.model_gateway.models import (
    ModelRequest,
    StreamEvent,
    StreamEventType,
)
from waxprep.model_gateway.providers.mock import MockProvider

FALLBACK_TEXT = "complete fallback answer"


class EmptyStreamProvider(MockProvider):
    """Completes the stream without ever emitting a text delta."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(
            type=StreamEventType.COMPLETED,
            provider_request_id="empty-stream-request",
        )


class MidStreamFailProvider(MockProvider):
    """Emits one delta, then fails mid-stream."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(type=StreamEventType.TEXT_DELTA, text="partial ")
        raise ModelGatewayError("mid-stream failure")


@pytest.mark.asyncio
async def test_empty_stream_falls_back_to_complete() -> None:
    world = make_model_teacher_world(
        provider=EmptyStreamProvider(response_text=FALLBACK_TEXT)
    )
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax,
        "explain photosynthesis",
        channel="whatsapp",
        channel_ref="wamid.EMPTY-STREAM-1",
    )
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == FALLBACK_TEXT
    # One stream attempt plus one complete fallback call.
    assert world.provider.calls == 2


@pytest.mark.asyncio
async def test_mid_stream_failure_falls_back_to_complete() -> None:
    world = make_model_teacher_world(
        provider=MidStreamFailProvider(response_text=FALLBACK_TEXT)
    )
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax,
        "explain gravity",
        channel="whatsapp",
        channel_ref="wamid.MID-STREAM-FAIL-1",
    )
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == FALLBACK_TEXT
    # The partial delta was discarded; the persisted reply is the
    # authoritative complete answer, not the streamed fragment.
    assert world.provider.calls == 2
