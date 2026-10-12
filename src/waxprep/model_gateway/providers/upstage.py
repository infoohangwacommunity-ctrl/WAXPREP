"""Upstage Solar adapter.

Upstage chat models are served on the OpenAI-compatible Chat Completions
API (`POST /v1/chat/completions`). The Responses API path used by OpenAI
is not available for the current Solar model IDs, so this provider talks
chat/completions directly while remaining a ModelProvider for the gateway.
"""

from __future__ import annotations

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
    ToolCall,
)
from waxprep.model_gateway.providers._http import raise_for_provider_status


class UpstageResponsesProvider:
    """Upstage provider identity + chat/completions transport."""

    name = "upstage"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.upstage.ai/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    def capabilities(self, model: str) -> frozenset[ModelCapability]:
        # Solar chat is text (+ tools). Do not claim image/file input for
        # Responses-style multimodal; Document Parse is a separate path.
        return frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.TOOL_CALLING,
                ModelCapability.STREAMING,
            }
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_messages(self, request: ModelRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.instructions:
            messages.append({"role": "system", "content": request.instructions})
        parts: list[str] = []
        for item in request.input:
            if item.type is ContentType.TEXT and item.text:
                parts.append(item.text)
        messages.append({"role": "user", "content": "\n".join(parts)})
        return messages

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": self._build_messages(request),
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.input_schema),
                    },
                }
                for tool in request.tools
            ]
        return payload

    def _parse_response(self, data: Any, request: ModelRequest) -> ModelResponse:
        choices = data.get("choices") or []
        text = ""
        tool_calls: list[ToolCall] = []
        finish = None
        if choices:
            choice0 = choices[0]
            finish = choice0.get("finish_reason")
            message = choice0.get("message") or {}
            text = str(message.get("content") or "")
            for tc in message.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or {}
                if isinstance(raw_args, str):
                    try:
                        import json as _json

                        raw_args = _json.loads(raw_args) if raw_args.strip() else {}
                    except Exception:
                        raw_args = {"_raw": raw_args}
                if not isinstance(raw_args, dict):
                    raw_args = {}
                tool_calls.append(
                    ToolCall(
                        id=str(tc.get("id") or ""),
                        name=str(fn.get("name") or ""),
                        arguments=raw_args,
                    )
                )
        usage_obj = data.get("usage") or {}
        usage = ModelUsage(
            input_tokens=_int_or_none(usage_obj.get("prompt_tokens")),
            output_tokens=_int_or_none(usage_obj.get("completion_tokens")),
            total_tokens=_int_or_none(usage_obj.get("total_tokens")),
            cached_input_tokens=_int_or_none(
                (usage_obj.get("prompt_tokens_details") or {}).get("cached_tokens")
            ),
            reasoning_tokens=_int_or_none(
                (usage_obj.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                )
            ),
        )
        return ModelResponse(
            text=text,
            tool_calls=tuple(tool_calls),
            usage=usage,
            provider=self.name,
            model=str(data.get("model") or request.model),
            provider_request_id=str(data["id"]) if data.get("id") is not None else None,
            finish_reason=str(finish) if finish is not None else None,
            raw_metadata={"provider": self.name, "api": "chat.completions"},
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self._build_payload(request)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=request.timeout_seconds,
            )
            raise_for_provider_status(response)
            return self._parse_response(response.json(), request)
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
                f"{self._base_url}/chat/completions",
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
                        import json

                        event = json.loads(raw)
                    except Exception:
                        continue
                    for delta in self._parse_stream_chunk(event):
                        yield delta
        except httpx.TimeoutException as exc:
            raise TimeoutError("provider request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("provider transport failed") from exc
        finally:
            if owns_client:
                await client.aclose()

    def _parse_stream_chunk(self, event: Any) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        if not isinstance(event, dict):
            return events
        for choice in event.get("choices") or []:
            delta = (choice.get("delta") or {}).get("content")
            if delta:
                events.append(
                    StreamEvent(type=StreamEventType.TEXT_DELTA, text=str(delta))
                )
            if choice.get("finish_reason"):
                events.append(StreamEvent(type=StreamEventType.COMPLETED))
        return events


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None
