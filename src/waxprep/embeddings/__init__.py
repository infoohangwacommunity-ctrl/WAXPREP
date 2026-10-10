"""Embedding providers (retrieval index, not memory)."""

from waxprep.embeddings.http import EmbeddingHTTPError, HttpEmbeddingProvider
from waxprep.embeddings.mock import DeterministicEmbeddingProvider
from waxprep.embeddings.similarity import cosine_similarity

__all__ = [
    "DeterministicEmbeddingProvider",
    "EmbeddingHTTPError",
    "HttpEmbeddingProvider",
    "cosine_similarity",
]
