# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnnecessaryCast=false
"""OpenAI-compatible HTTP embedding provider (no SDK)."""

from __future__ import annotations

from typing import Any, cast

import httpx

from waxprep.embeddings.similarity import validate_embedding


class EmbeddingHTTPError(RuntimeError):
    """Raised when a live embedding request fails."""


class HttpEmbeddingProvider:
    """Production-capable embedding client via OpenAI-compatible /embeddings."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        dimensions: int | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key is required")
        if not model.strip():
            raise ValueError("model is required")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._dimensions = dimensions
        self._timeout = timeout_seconds
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            raise RuntimeError("dimensions unknown until first embed or config")
        return self._dimensions

    async def embed(self, text: str) -> tuple[float, ...]:
        results = await self.embed_many([text])
        return results[0]

    async def embed_many(self, texts: list[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        for t in texts:
            if not t.strip():
                raise ValueError("cannot embed empty text")
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions

        owns = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                f"{self._base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingHTTPError("embedding request timed out") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingHTTPError("embedding transport failed") from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code >= 400:
            raise EmbeddingHTTPError(
                f"embedding provider error {response.status_code}: "
                f"{response.text[:200]}"
            )
        data = cast(dict[str, Any], response.json())
        items_obj = data.get("data")
        if not isinstance(items_obj, list) or len(items_obj) != len(texts):
            raise EmbeddingHTTPError("malformed embedding response")
        items = cast(list[Any], items_obj)
        vectors: list[tuple[float, ...]] = []
        for item in items:
            if not isinstance(item, dict):
                raise EmbeddingHTTPError("malformed embedding item")
            item_dict = cast(dict[str, Any], item)
            raw = item_dict.get("embedding")
            if not isinstance(raw, list):
                raise EmbeddingHTTPError("missing embedding vector")
            floats = [float(cast(Any, x)) for x in cast(list[Any], raw)]
            vec = validate_embedding(floats, expected_dims=self._dimensions)
            if self._dimensions is None:
                self._dimensions = len(vec)
            vectors.append(vec)
        return vectors
