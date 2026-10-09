"""Tool contracts for Context Intelligence (application executes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from waxprep.context.models import Evidence
from waxprep.domain.identifiers import WaxId


@dataclass(frozen=True, slots=True)
class ToolResult:
    name: str
    results: tuple[Evidence, ...]
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


class ContextTool(Protocol):
    name: str

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult: ...
