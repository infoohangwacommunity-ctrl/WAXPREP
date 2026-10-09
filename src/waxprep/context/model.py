"""Context Intelligence model protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ContextModelRequest:
    system_prompt: str
    user_query: str
    evidence: str
    tools: list[dict[str, Any]]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ContextModelDecision:
    action: str
    arguments: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class ContextModelFinal:
    summary: str
    selected_evidence_ids: tuple[str, ...]
    uncertainty: tuple[str, ...]
    stopped_reason: str


class ContextModel(Protocol):
    async def decide(self, request: ContextModelRequest) -> ContextModelDecision: ...

    async def finalize(self, request: ContextModelRequest) -> ContextModelFinal: ...
