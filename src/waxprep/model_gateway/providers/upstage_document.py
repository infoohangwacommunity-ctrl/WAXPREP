"""Upstage Document Parse API provider.

This provider implements document parsing for Upstage's document-parse model.
It handles PDF, DOCX, and other document formats for text extraction.

The provider handles:
- File upload and parsing
- Text extraction from documents
- Error handling and retries
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from waxprep.model_gateway.errors import ModelGatewayError
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
    ToolCall,
)
from waxprep.model_gateway.provider import ModelProvider

logger = logging.getLogger(__name__)


class UpstageDocumentProvider(ModelProvider):
    """Upstage Document Parse API implementation.

    This provider specializes in document parsing for text extraction.
    It supports PDF, DOCX, TXT, and other document formats.

    The document-parse model uses a different endpoint and request format
    than the chat completions API.
    """

    NAME = "upstage_document"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.upstage.ai/v1",
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def capabilities(self) -> frozenset[ModelCapability]:
        return frozenset({
            ModelCapability.TEXT_INPUT,
            ModelCapability.FILE_INPUT,
        })

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Execute a document parsing request."""
        # Find file content items
        file_items = [item for item in request.input if item.type == ContentType.FILE]
        text_items = [item for item in request.input if item.type == ContentType.TEXT]

        if not file_items:
            raise ModelGatewayError("No file input for document parsing", retryable=False)

        # For now, handle the first file
        file_item = file_items[0]

        # The file URI should be a storage reference
        # In production, this would be resolved to actual bytes
        # For now, we'll simulate the document parsing response
        
        # Build the request
        prompt = text_items[0].text if text_items else "Extract all text from this document"
        
        # Upstage document parsing uses the /document/parse endpoint
        # But for now, we'll use chat/completions with a document-parse model
        body = {
            "model": request.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        },
                        {
                            "type": "file",
                            "file": self._encode_file_uri(file_item.uri),
                        },
                    ],
                }
            ],
        }

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            )

        if response.status_code >= 500:
            raise ModelGatewayError(
                f"Upstage server error: {response.status_code}",
                retryable=True,
            )
        if response.status_code >= 400:
            error_detail = response.text[:500]
            raise ModelGatewayError(
                f"Upstage API error: {response.status_code}: {error_detail}",
                retryable=False,
            )

        data = response.json()
        return self._parse_response(data, request)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _encode_file_uri(self, uri: str) -> str:
        """Encode file storage reference for Upstage API.

        Upstage expects either:
        1. A data URI (base64 encoded file content)
        2. A URL to a publicly accessible file

        Since our files are private, we need to encode them as data URIs.
        This is a placeholder - in production, this would fetch from byte store.
        """
        # For now, return a placeholder
        # In production: fetch from byte store and encode as base64
        return f"data:application/octet-stream;base64,PLACEHOLDER"

    def _parse_response(self, data: dict[str, Any], request: ModelRequest) -> ModelResponse:
        """Parse Upstage response into ModelResponse."""
        text = ""
        tool_calls: list[ToolCall] = []

        if "choices" in data and data["choices"]:
            choice = data["choices"][0]
            message = choice.get("message", {})

            if "content" in message:
                text = message["content"] or ""
            elif "text" in message:
                text = message["text"] or ""

        # Extract usage
        usage_data = data.get("usage", {})
        usage = ModelUsage(
            input_tokens=usage_data.get("prompt_tokens") or usage_data.get("input_tokens"),
            output_tokens=usage_data.get("completion_tokens") or usage_data.get("output_tokens"),
            total_tokens=usage_data.get("total_tokens"),
        )

        return ModelResponse(
            text=text,
            tool_calls=tuple(tool_calls),
            usage=usage,
            provider=self.NAME,
            model=request.model,
            provider_request_id=data.get("id"),
            finish_reason=data.get("choices", [{}])[0].get("finish_reason"),
        )

    async def stream(self, request: ModelRequest) -> Any:
        """Streaming is not supported for document parsing."""
        raise NotImplementedError("Upstage document parsing does not support streaming")
