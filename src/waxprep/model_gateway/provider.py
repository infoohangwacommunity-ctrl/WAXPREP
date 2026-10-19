"""Provider adapter protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from waxprep.model_gateway.models import (
    ModelCapability,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)


class ModelProvider(Protocol):
    """Contract implemented by every model provider adapter."""

    @property
    def name(self) -> str:
        """Stable provider identifier."""
        ...

    def capabilities(self, model: str) -> frozenset[ModelCapability]:
        """Return capabilities supported by this provider/model."""
        ...

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Execute a non-streaming model request."""
        ...

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Execute a streaming model request."""
        ...
