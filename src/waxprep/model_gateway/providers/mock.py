"""Deterministic provider for normal tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from waxprep.model_gateway.errors import ModelGatewayError
from waxprep.model_gateway.models import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StreamEvent,
    StreamEventType,
)


class MockProvider:
    """Predictable provider that never makes external calls."""

    name = "mock"

    def __init__(
        self,
        *,
        response_text: str = "mock response",
        fail_times: int = 0,
        fail_error: ModelGatewayError | None = None,
    ) -> None:
        self.response_text = response_text
        self.fail_times = fail_times
        self.fail_error = fail_error or ModelGatewayError("mock failure")
        self.calls = 0

    def capabilities(self, model: str) -> frozenset[ModelCapability]:
        return frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.AUDIO_INPUT,
                ModelCapability.FILE_INPUT,
                ModelCapability.TOOL_CALLING,
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.STREAMING,
            }
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.fail_error
        return ModelResponse(
            text=self.response_text,
            tool_calls=(),
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            provider=self.name,
            model=request.model,
            provider_request_id="mock-request",
            finish_reason="stop",
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.fail_error
        for part in self.response_text.split():
            await asyncio.sleep(0)
            yield StreamEvent(type=StreamEventType.TEXT_DELTA, text=part + " ")
        yield StreamEvent(
            type=StreamEventType.USAGE,
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        yield StreamEvent(
            type=StreamEventType.COMPLETED,
            provider_request_id="mock-stream-request",
        )
