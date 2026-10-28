"""Production embedding wiring: settings -> provider -> service graph.

The semantic retrieval capability already exists; these tests pin the
wiring that turns it on in production: settings parse an optional
OpenAI-compatible provider, a misconfigured half fails loudly at
startup, and the app builds the provider only when fully configured.
"""

from __future__ import annotations

import pytest

from waxprep.app import build_embedder
from waxprep.embeddings.http import HttpEmbeddingProvider
from waxprep.settings import Settings, load_settings


def test_embedding_settings_default_off() -> None:
    s = load_settings({})
    assert s.embedding_api_key is None
    assert s.embedding_model is None
    assert s.embedding_base_url == "https://api.openai.com/v1"
    assert s.embedding_dimensions is None


def test_embedding_settings_parsed() -> None:
    s = load_settings(
        {
            "WAXPREP_EMBEDDING_API_KEY": "sk-test",
            "WAXPREP_EMBEDDING_MODEL": "text-embedding-3-small",
            "WAXPREP_EMBEDDING_BASE_URL": "https://embed.example.com/v1",
            "WAXPREP_EMBEDDING_DIMENSIONS": "512",
        }
    )
    assert s.embedding_api_key == "sk-test"
    assert s.embedding_model == "text-embedding-3-small"
    assert s.embedding_base_url == "https://embed.example.com/v1"
    assert s.embedding_dimensions == 512


def test_embedding_model_without_key_fails_loudly() -> None:
    with pytest.raises(ValueError, match="WAXPREP_EMBEDDING_API_KEY"):
        load_settings({"WAXPREP_EMBEDDING_MODEL": "text-embedding-3-small"})


def test_embedding_key_without_model_fails_loudly() -> None:
    with pytest.raises(ValueError, match="WAXPREP_EMBEDDING_MODEL"):
        load_settings({"WAXPREP_EMBEDDING_API_KEY": "sk-test"})


def test_build_embedder_is_none_when_unset() -> None:
    assert build_embedder(Settings()) is None


def test_build_embedder_builds_http_provider() -> None:
    provider = build_embedder(
        Settings(
            embedding_api_key="sk-test",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=512,
        )
    )
    assert isinstance(provider, HttpEmbeddingProvider)
    assert provider.model == "text-embedding-3-small"
    assert provider.dimensions == 512
