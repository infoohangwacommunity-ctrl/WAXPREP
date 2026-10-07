"""Central model execution policy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from waxprep.model_gateway.errors import (
    ModelGatewayError,
    UnsupportedCapabilityError,
)
from waxprep.model_gateway.models import (
    ContentType,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from waxprep.model_gateway.registry import ModelRegistry

logger = logging.getLogger(__name__)


class ModelGateway:
    """Provider-neutral execution boundary."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        max_retries: int = 2,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._registry = registry
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds

    async def complete(self, request: ModelRequest) -> ModelResponse:
        config, provider = self._registry.resolve(request.model)
        self._validate_request_capabilities(request, config.capabilities)
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                return await provider.complete(request)
            except ModelGatewayError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
                logger.warning(
                    "retrying model request",
                    extra={
                        "wax_id": str(request.wax_id),
                        "provider": provider.name,
                        "model": config.model,
                        "attempt": attempt + 1,
                    },
                )
                await asyncio.sleep(self._retry_delay_seconds * (2**attempt))
        raise AssertionError("unreachable")

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        config, provider = self._registry.resolve(request.model)
        self._validate_request_capabilities(request, config.capabilities)
        if ModelCapability.STREAMING not in config.capabilities:
            raise UnsupportedCapabilityError(
                f"model {request.model!r} does not support streaming"
            )
        async for event in provider.stream(request):
            yield event

    @staticmethod
    def _validate_request_capabilities(
        request: ModelRequest,
        capabilities: frozenset[ModelCapability],
    ) -> None:
        types = {item.type for item in request.input}
        if (
            ContentType.IMAGE in types
            and ModelCapability.IMAGE_INPUT not in capabilities
        ):
            raise UnsupportedCapabilityError("model does not support image input")
        if (
            ContentType.AUDIO in types
            and ModelCapability.AUDIO_INPUT not in capabilities
        ):
            raise UnsupportedCapabilityError("model does not support audio input")
        if ContentType.FILE in types and ModelCapability.FILE_INPUT not in capabilities:
            raise UnsupportedCapabilityError("model does not support file input")
        if request.tools and ModelCapability.TOOL_CALLING not in capabilities:
            raise UnsupportedCapabilityError("model does not support tool calling")
        if request.stream and ModelCapability.STREAMING not in capabilities:
            raise UnsupportedCapabilityError("model does not support streaming")
