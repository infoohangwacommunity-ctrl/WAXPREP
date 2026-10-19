# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Anthropic Messages API adapter (HTTP, no SDK)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from waxprep.model_gateway.errors import (
    ProviderUnavailableError,
    TimeoutError,
)
from waxprep.model_gateway.models import (
    ContentType,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StreamEvent,
    StreamEventType,
)
from waxprep.model_gateway.providers._http import raise_for_provider_status


class AnthropicMessagesProvider:
    """Anthropic Messages API adapter."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        client: httpx.AsyncClient | None = None,
        api_version: str = "2023-06-01",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._api_version = api_version

    def capabilities(self, model: str) -> frozenset[ModelCapability]:
        return frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.TOOL_CALLING,
                ModelCapability.STREAMING,
            }
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self._build_payload(request)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                f"{self._base_url}/messages",
                headers=self._headers(),
                json=payload,
                timeout=request.timeout_seconds,
            )
            raise_for_provider_status(response)
            data = response.json()
            return self._parse_response(data, request)
        except httpx.TimeoutException as exc:
            raise TimeoutError("provider request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("provider transport failed") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        payload = self._build_payload(request)
        payload["stream"] = True
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/messages",
                headers=self._headers(),
                json=payload,
                timeout=request.timeout_seconds,
            ) as response:
                raise_for_provider_status(response)
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        yield StreamEvent(type=StreamEventType.COMPLETED)
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for parsed in self._parse_stream_event(event):
                        yield parsed
        except httpx.TimeoutException as exc:
            raise TimeoutError("provider stream timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("provider stream transport failed") from exc
        finally:
            if owns_client:
                await client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._api_version,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _build_payload(request: ModelRequest) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for item in request.input:
            if item.type is ContentType.TEXT:
                content.append({"type": "text", "text": item.text})
            else:
                content.append(
                    {
                        "type": item.type.value,
                        "source": {"type": "url", "url": item.uri},
                    }
                )
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": request.max_output_tokens or 1024,
        }
        if request.instructions:
            payload["system"] = request.instructions
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    def _parse_response(self, data: Any, request: ModelRequest) -> ModelResponse:
        parts: list[str] = []
        content = data.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
        usage_obj = data.get("usage")
        usage_raw: dict[str, Any] = (
            cast(dict[str, Any], usage_obj) if isinstance(usage_obj, dict) else {}
        )
        input_tokens = usage_raw.get("input_tokens")
        output_tokens = usage_raw.get("output_tokens")
        in_tok = int(input_tokens) if isinstance(input_tokens, int) else None
        out_tok = int(output_tokens) if isinstance(output_tokens, int) else None
        total = (
            (in_tok + out_tok) if in_tok is not None and out_tok is not None else None
        )
        request_id = data.get("id")
        stop = data.get("stop_reason")
        return ModelResponse(
            text="".join(parts),
            tool_calls=(),
            usage=ModelUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=total,
            ),
            provider=self.name,
            model=request.model,
            provider_request_id=str(request_id) if request_id is not None else None,
            finish_reason=str(stop) if stop is not None else None,
            raw_metadata={"provider": self.name},
        )

    def _parse_stream_event(self, event: Any) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        if not isinstance(event, dict):
            return events
        ev = cast(dict[str, Any], event)
        event_type = ev.get("type")
        if event_type == "content_block_delta":
            delta_obj = ev.get("delta")
            if isinstance(delta_obj, dict):
                delta = cast(dict[str, Any], delta_obj)
                if delta.get("type") == "text_delta":
                    events.append(
                        StreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            text=str(delta.get("text") or ""),
                        )
                    )
        elif event_type == "message_stop":
            events.append(StreamEvent(type=StreamEventType.COMPLETED))
        return events
