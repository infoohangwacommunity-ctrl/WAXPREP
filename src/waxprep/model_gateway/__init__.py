"""Wax Prep model gateway."""

from waxprep.model_gateway.errors import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    ModelGatewayError,
    ProviderUnavailableError,
    RateLimitError,
    RoleMismatchError,
    TimeoutError,
    UnsupportedCapabilityError,
)
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
    StreamEvent,
    ToolCall,
    ToolDefinition,
)
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry

__all__ = [
    "AuthenticationError",
    "ContentItem",
    "ContentType",
    "ContextLengthError",
    "InvalidRequestError",
    "ModelCapability",
    "ModelConfig",
    "ModelGateway",
    "ModelGatewayError",
    "ModelRegistry",
    "ModelRequest",
    "ModelResponse",
    "ModelRole",
    "ModelUsage",
    "ProviderUnavailableError",
    "RateLimitError",
    "RoleMismatchError",
    "StreamEvent",
    "TimeoutError",
    "ToolCall",
    "ToolDefinition",
    "UnsupportedCapabilityError",
]
