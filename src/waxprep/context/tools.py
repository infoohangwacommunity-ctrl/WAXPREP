"""Tool contracts for Context Intelligence (the application executes them)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from waxprep.context.models import Evidence
from waxprep.domain.identifiers import WaxId


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Everything a tool needs: who, what the model asked, and when it is."""

    wax_id: WaxId
    arguments: dict[str, Any]
    now: datetime
    trigger_entry_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    name: str
    results: tuple[Evidence, ...]
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


class ContextTool(Protocol):
    name: str

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...
