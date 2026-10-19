"""Model registry tests."""

from __future__ import annotations

import pytest

from waxprep.model_gateway.models import ModelRole
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry


def test_registry_resolves_provider() -> None:
    registry = ModelRegistry()
    provider = MockProvider()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider="mock",
            model="model-a",
            role=ModelRole.TEACHER,
            capabilities=provider.capabilities("model-a"),
        )
    )
    config, resolved = registry.resolve("teacher")
    assert config.model == "model-a"
    assert resolved is provider


def test_registry_rejects_unknown_provider() -> None:
    registry = ModelRegistry()
    with pytest.raises(ValueError):
        registry.register_model(
            ModelConfig(
                name="teacher",
                provider="missing",
                model="model-a",
                role=ModelRole.TEACHER,
                capabilities=frozenset(),
            )
        )


def test_registry_rejects_duplicate_provider() -> None:
    registry = ModelRegistry()
    registry.register_provider(MockProvider())
    with pytest.raises(ValueError):
        registry.register_provider(MockProvider())
