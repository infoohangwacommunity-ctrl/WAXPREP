"""GatewayContextModel parses structured decisions via Model Gateway."""

from __future__ import annotations

import pytest

from waxprep.context.gateway_model import GatewayContextModel
from waxprep.context.model import ContextModelRequest
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import ModelRole
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry


@pytest.mark.asyncio
async def test_gateway_context_model_parses_stop_decision() -> None:
    provider = MockProvider(
        response_text='{"action":"stop","arguments":{},"reason":"nothing needed"}'
    )
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="context",
            provider="mock",
            model="mock-context",
            role=ModelRole.CONTEXT,
            capabilities=provider.capabilities("mock-context"),
        )
    )
    gateway = ModelGateway(registry)
    model = GatewayContextModel(gateway, model_name="context")
    decision = await model.decide(
        ContextModelRequest(
            system_prompt="sys",
            user_query="hello",
            evidence="(none)",
            tools=[],
            max_output_tokens=200,
        )
    )
    assert decision.action == "stop"
    assert "nothing" in decision.reason
