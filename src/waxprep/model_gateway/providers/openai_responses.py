# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Adapter for OpenAI Responses-compatible APIs (HTTP, no SDK)."""

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


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

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
                ModelCapability.REASONING,
            }
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self._build_payload(request)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                f"{self._base_url}/responses",
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
                f"{self._base_url}/responses",
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
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        raise_for_provider_status(response)

    @staticmethod
    def _build_payload(request: ModelRequest) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        for item in request.input:
            if item.type is ContentType.TEXT:
                input_items.append({"type": "input_text", "text": item.text})
            else:
                input_items.append(
                    {
                        "type": f"input_{item.type.value}",
                        "uri": item.uri,
                        "mime_type": item.mime_type,
                    }
                )
        payload: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
        }
        if request.instructions:
            payload["instructions"] = request.instructions
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                }
                for tool in request.tools
            ]
        return payload

    def _parse_response(self, data: Any, request: ModelRequest) -> ModelResponse:
        text = str(data.get("output_text") or "")
        usage_obj = data.get("usage")
        usage_raw: dict[str, Any] = (
            cast(dict[str, Any], usage_obj) if isinstance(usage_obj, dict) else {}
        )
        usage = ModelUsage(
            input_tokens=_int_or_none(usage_raw.get("input_tokens")),
            output_tokens=_int_or_none(usage_raw.get("output_tokens")),
            total_tokens=_int_or_none(usage_raw.get("total_tokens")),
            cached_input_tokens=_int_or_none(usage_raw.get("cached_input_tokens")),
            reasoning_tokens=_int_or_none(usage_raw.get("reasoning_tokens")),
        )
        request_id = data.get("id")
        finish = data.get("status") or data.get("finish_reason")
        return ModelResponse(
            text=text,
            tool_calls=(),
            usage=usage,
            provider=self.name,
            model=request.model,
            provider_request_id=str(request_id) if request_id is not None else None,
            finish_reason=str(finish) if finish is not None else None,
            raw_metadata={"provider": self.name},
        )

    def _parse_stream_event(self, event: Any) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        if not isinstance(event, dict):
            return events
        ev = cast(dict[str, Any], event)
        event_type = ev.get("type")
        if event_type in ("response.output_text.delta", "content.delta"):
            delta = ev.get("delta")
            if delta is None:
                delta = ev.get("text")
            events.append(
                StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text="" if delta is None else str(delta),
                )
            )
        elif event_type in ("response.completed", "response.done"):
            rid = ev.get("id")
            if rid is None:
                rid = ev.get("response_id")
            events.append(
                StreamEvent(
                    type=StreamEventType.COMPLETED,
                    provider_request_id=None if rid is None else str(rid),
                )
            )
        return events


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
