"""Embedding provider contract (retrieval index, not memory)."""

from __future__ import annotations

from typing import Protocol


class EmbeddingProvider(Protocol):
    """Produces dense vectors for semantic candidate retrieval."""

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, text: str) -> tuple[float, ...]: ...

    async def embed_many(self, texts: list[str]) -> list[tuple[float, ...]]: ...
