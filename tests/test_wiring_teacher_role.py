"""Composition-time validation of the Teacher logical model role."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from support import TEST_CIPHER
from waxprep.clock import FakeClock
from waxprep.context.mock_model import MockContextModel
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import ModelCapability, ModelRole
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry
from waxprep.storage.memory import InMemoryStorage
from waxprep.wiring import build_services


def test_build_services_rejects_teacher_registered_as_context() -> None:
    provider = MockProvider(response_text="x")
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider="mock",
            model="mock-model",
            role=ModelRole.CONTEXT,
            capabilities=frozenset({ModelCapability.TEXT_INPUT}),
        )
    )
    gateway = ModelGateway(registry)
    with pytest.raises(ValueError, match="expected 'teacher'"):
        build_services(
            storage=InMemoryStorage(),
            cipher=TEST_CIPHER,
            clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
            context_model=MockContextModel(),
            gateway=gateway,
        )
