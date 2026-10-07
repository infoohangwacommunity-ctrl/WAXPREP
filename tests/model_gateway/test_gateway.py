"""Gateway policy tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from waxprep.model_gateway.errors import RateLimitError, UnsupportedCapabilityError
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelCapability,
    ModelRequest,
    ModelRole,
    StreamEventType,
)
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry


@pytest.mark.asyncio
async def test_gateway_executes_mock_request() -> None:
    provider = MockProvider(response_text="test answer")
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider="mock",
            model="mock-model",
            role=ModelRole.TEACHER,
            capabilities=provider.capabilities("mock-model"),
        )
    )
    gateway = ModelGateway(registry)
    response = await gateway.complete(
        ModelRequest(
            wax_id=uuid4(),
            role=ModelRole.TEACHER,
            model="teacher",
            instructions=None,
            input=(ContentItem(type=ContentType.TEXT, text="Hello"),),
        )
    )
    assert response.text == "test answer"
    assert response.provider == "mock"
    assert response.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_gateway_retries_retryable_error() -> None:
    provider = MockProvider(fail_times=1, fail_error=RateLimitError("temporary"))
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider="mock",
            model="mock-model",
            role=ModelRole.TEACHER,
            capabilities=provider.capabilities("mock-model"),
        )
    )
    gateway = ModelGateway(registry, max_retries=1, retry_delay_seconds=0)
    response = await gateway.complete(
        ModelRequest(
            wax_id=uuid4(),
            role=ModelRole.TEACHER,
            model="teacher",
            instructions=None,
            input=(),
        )
    )
    assert response.text == "mock response"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_gateway_rejects_unsupported_image() -> None:
    provider = MockProvider()
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="text-only",
            provider="mock",
            model="text-only",
            role=ModelRole.TEACHER,
            capabilities=frozenset({ModelCapability.TEXT_INPUT}),
        )
    )
    gateway = ModelGateway(registry)
    with pytest.raises(UnsupportedCapabilityError):
        await gateway.complete(
            ModelRequest(
                wax_id=uuid4(),
                role=ModelRole.TEACHER,
                model="text-only",
                instructions=None,
                input=(
                    ContentItem(type=ContentType.IMAGE, uri="https://example.com/i"),
                ),
            )
        )


@pytest.mark.asyncio
async def test_gateway_streams_normalized_events() -> None:
    provider = MockProvider(response_text="hello world")
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider="mock",
            model="mock-model",
            role=ModelRole.TEACHER,
            capabilities=provider.capabilities("mock-model"),
        )
    )
    gateway = ModelGateway(registry)
    events = [
        event
        async for event in gateway.stream(
            ModelRequest(
                wax_id=uuid4(),
                role=ModelRole.TEACHER,
                model="teacher",
                instructions=None,
                input=(),
                stream=True,
            )
        )
    ]
    assert events[0].type is StreamEventType.TEXT_DELTA
    assert events[-1].type is StreamEventType.COMPLETED
