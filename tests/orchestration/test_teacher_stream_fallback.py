"""Teacher streaming fallbacks: empty stream, mid-stream failure, and a
silently cut stream.

A stream can complete without any usable text delta, die halfway
through, or — the live-incident shape — simply end mid-sentence with no
completion event and no exception when the provider closes the
connection early. All of these must fall back to one complete gateway
call and still produce an authoritative tutor reply — never silence,
never a partial answer presented as final.
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


class SilentCutProvider(MockProvider):
    """Streams one delta, then just ends: no completion event, no error.

    This is the live-incident shape — a TCP connection closed
    mid-sentence with no [DONE] from the provider. The async iterator
    exits cleanly, so the teacher must not mistake the fragment for a
    complete answer.
    """

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(
            type=StreamEventType.TEXT_DELTA,
            text="Yeah, I do have interactive buttons! That's what the",
        )


class ErrorEventProvider(MockProvider):
    """Emits one delta, then an explicit ERROR event mid-stream."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(type=StreamEventType.TEXT_DELTA, text="partial ")
        yield StreamEvent(type=StreamEventType.ERROR, error="provider cut the stream")


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


@pytest.mark.asyncio
async def test_silent_cut_stream_falls_back_to_complete() -> None:
    """Live incident regression: the provider's connection closed
    mid-sentence with no completion event, and the partial fragment was
    persisted and delivered as the final answer. The guard must discard
    the fragment and take the authoritative complete() text instead."""
    world = make_model_teacher_world(
        provider=SilentCutProvider(response_text=FALLBACK_TEXT)
    )
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax,
        "do you have interactive buttons",
        channel="whatsapp",
        channel_ref="wamid.SILENT-CUT-1",
    )
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == FALLBACK_TEXT
    # One stream attempt plus one complete fallback call — the streamed
    # fragment was never persisted as the final answer.
    assert world.provider.calls == 2


@pytest.mark.asyncio
async def test_error_event_stream_falls_back_to_complete() -> None:
    world = make_model_teacher_world(
        provider=ErrorEventProvider(response_text=FALLBACK_TEXT)
    )
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax,
        "explain gravity",
        channel="whatsapp",
        channel_ref="wamid.ERROR-EVENT-1",
    )
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == FALLBACK_TEXT
    assert world.provider.calls == 2


@pytest.mark.asyncio
async def test_clean_stream_uses_streamed_text_without_fallback() -> None:
    """A stream that ends WITH a completion event is used as-is: no
    wasteful second complete call on the healthy path."""
    world = make_model_teacher_world(provider=MockProvider(response_text=FALLBACK_TEXT))
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax,
        "explain photosynthesis",
        channel="whatsapp",
        channel_ref="wamid.CLEAN-STREAM-1",
    )
    assert result.tutor_entry is not None
    assert result.tutor_entry.text == FALLBACK_TEXT
    # One streamed call only — the completion event means no fallback.
    assert world.provider.calls == 1
