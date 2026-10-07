"""HTTP adapter translation tests (no live network)."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
)
from waxprep.model_gateway.providers.anthropic import AnthropicMessagesProvider
from waxprep.model_gateway.providers.openai_responses import OpenAIResponsesProvider
from waxprep.model_gateway.providers.upstage import UpstageResponsesProvider


@pytest.mark.asyncio
async def test_openai_adapter_translates_request() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "output_text": "hello",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesProvider(
        api_key="test",
        base_url="https://example.test/v1",
        client=client,
    )
    response = await provider.complete(
        ModelRequest(
            wax_id=uuid4(),
            role=ModelRole.TEACHER,
            model="test-model",
            instructions="Tutor",
            input=(ContentItem(type=ContentType.TEXT, text="Hello"),),
        )
    )
    assert response.text == "hello"
    assert response.usage.total_tokens == 5
    assert '"model":"test-model"' in str(captured["json"])
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_adapter_translates_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicMessagesProvider(
        api_key="test",
        base_url="https://example.test/v1",
        client=client,
    )
    response = await provider.complete(
        ModelRequest(
            wax_id=uuid4(),
            role=ModelRole.CONTEXT,
            model="claude-test",
            instructions="Context operator",
            input=(ContentItem(type=ContentType.TEXT, text="Find useful context."),),
        )
    )
    assert response.text == "hello"
    assert response.provider == "anthropic"
    assert response.usage.total_tokens == 7
    await client.aclose()


def test_upstage_has_independent_provider_identity() -> None:
    provider = UpstageResponsesProvider(api_key="test")
    assert provider.name == "upstage"
    assert provider.capabilities("solar-test")
