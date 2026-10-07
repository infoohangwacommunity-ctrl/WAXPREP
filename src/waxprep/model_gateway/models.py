"""Provider-neutral model gateway contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


def _empty_any_map() -> dict[str, Any]:
    return {}


def _empty_str_map() -> dict[str, str]:
    return {}


class ModelRole(StrEnum):
    """Logical role for a model inside Wax Prep."""

    TEACHER = "teacher"
    CONTEXT = "context"


class ContentType(StrEnum):
    """Provider-neutral content representations."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"


class ModelCapability(StrEnum):
    """Capabilities a model endpoint may advertise."""

    TEXT_INPUT = "text_input"
    IMAGE_INPUT = "image_input"
    AUDIO_INPUT = "audio_input"
    FILE_INPUT = "file_input"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    REASONING = "reasoning"


@dataclass(frozen=True, slots=True)
class ContentItem:
    """A provider-neutral input item."""

    type: ContentType
    text: str | None = None
    uri: str | None = None
    mime_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=_empty_any_map)

    def __post_init__(self) -> None:
        if self.type is ContentType.TEXT:
            if not self.text:
                raise ValueError("text content requires text")
            return
        if not self.uri:
            raise ValueError(f"{self.type.value} content requires uri")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Provider-neutral tool declaration."""

    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name cannot be empty")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation returned by a model."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Provider-neutral model request."""

    wax_id: UUID
    role: ModelRole
    model: str
    instructions: str | None
    input: tuple[ContentItem, ...]
    tools: tuple[ToolDefinition, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=_empty_str_map)
    max_output_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Normalized provider usage (no monetary pricing)."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.total_tokens,
        ):
            if value is not None and value < 0:
                raise ValueError("usage values cannot be negative")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Normalized non-streaming model response."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    usage: ModelUsage
    provider: str
    model: str
    provider_request_id: str | None
    finish_reason: str | None
    raw_metadata: Mapping[str, Any] = field(default_factory=_empty_any_map)


class StreamEventType(StrEnum):
    """Normalized streaming event categories."""

    TEXT_DELTA = "text_delta"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    USAGE = "usage"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """Provider-neutral streaming event."""

    type: StreamEventType
    text: str | None = None
    tool_call: ToolCall | None = None
    usage: ModelUsage | None = None
    error: str | None = None
    provider_request_id: str | None = None
