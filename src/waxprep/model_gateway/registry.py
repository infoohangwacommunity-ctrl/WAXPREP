"""Provider and model registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from waxprep.model_gateway.models import ModelCapability, ModelRole
from waxprep.model_gateway.provider import ModelProvider


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Configuration for one logical model deployment."""

    name: str
    provider: str
    model: str
    role: ModelRole
    capabilities: frozenset[ModelCapability]


class ModelRegistry:
    """Resolves logical Wax Prep models to provider adapters."""

    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._models: dict[str, ModelConfig] = {}

    def register_provider(self, provider: ModelProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def register_model(self, config: ModelConfig) -> None:
        if config.name in self._models:
            raise ValueError(f"model already registered: {config.name}")
        if config.provider not in self._providers:
            raise ValueError(f"unknown provider: {config.provider}")
        self._models[config.name] = config

    def get_model(self, name: str) -> ModelConfig:
        try:
            return self._models[name]
        except KeyError as exc:
            raise KeyError(f"unknown model: {name}") from exc

    def get_provider(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {name}") from exc

    def resolve(self, model_name: str) -> tuple[ModelConfig, ModelProvider]:
        config = self.get_model(model_name)
        provider = self.get_provider(config.provider)
        return config, provider

    def models(self) -> Mapping[str, ModelConfig]:
        return dict(self._models)

    def providers(self) -> Mapping[str, ModelProvider]:
        return dict(self._providers)
