"""Upstage Solar adapter (OpenAI-compatible Responses/Chat HTTP)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from waxprep.model_gateway.models import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from waxprep.model_gateway.providers.openai_responses import OpenAIResponsesProvider


class UpstageResponsesProvider(OpenAIResponsesProvider):
    """Upstage provider with independent identity and default base URL."""

    name = "upstage"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.upstage.ai/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, client=client)

    def capabilities(self, model: str) -> frozenset[ModelCapability]:
        return frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.FILE_INPUT,
                ModelCapability.TOOL_CALLING,
                ModelCapability.STREAMING,
            }
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return ModelResponse(
            text=response.text,
            tool_calls=response.tool_calls,
            usage=response.usage,
            provider=self.name,
            model=response.model,
            provider_request_id=response.provider_request_id,
            finish_reason=response.finish_reason,
            raw_metadata=dict(response.raw_metadata) | {"provider": self.name},
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        async for event in super().stream(request):
            yield event
