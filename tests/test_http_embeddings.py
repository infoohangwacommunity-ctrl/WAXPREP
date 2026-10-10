"""HTTP embedding provider tests (mocked transport)."""

from __future__ import annotations

import httpx
import pytest

from waxprep.embeddings.http import EmbeddingHTTPError, HttpEmbeddingProvider


@pytest.mark.asyncio
async def test_http_embedding_parses_vectors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.0, 1.0, 0.0]}, {"embedding": [1.0, 0.0, 0.0]}]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpEmbeddingProvider(
        api_key="test",
        model="text-embedding-test",
        base_url="https://example.test/v1",
        dimensions=3,
        client=client,
    )
    vectors = await provider.embed_many(["a", "b"])
    assert len(vectors) == 2
    assert vectors[0] == (0.0, 1.0, 0.0)
    await client.aclose()


@pytest.mark.asyncio
async def test_http_embedding_rejects_error_status() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="fail")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpEmbeddingProvider(
        api_key="test",
        model="text-embedding-test",
        client=client,
    )
    with pytest.raises(EmbeddingHTTPError):
        await provider.embed("hello")
    await client.aclose()
