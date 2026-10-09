"""Embedding provider contract tests."""

import pytest

from waxprep.embeddings.mock import DeterministicEmbeddingProvider


@pytest.mark.asyncio
async def test_mock_embedding_is_deterministic() -> None:
    provider = DeterministicEmbeddingProvider(dimensions=16)
    first = await provider.embed("hello wax prep")
    second = await provider.embed("hello wax prep")
    assert first == second
    assert len(first) == 16
