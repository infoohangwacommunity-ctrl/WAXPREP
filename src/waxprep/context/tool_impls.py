"""Storage-backed Context Intelligence tools (read-only, WAX-ID scoped)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from waxprep.context.retrieval import HybridRetriever
from waxprep.context.tools import ToolResult
from waxprep.domain.identifiers import WaxId
from waxprep.embeddings.protocol import EmbeddingProvider


def _limit(arguments: dict[str, Any], default: int = 8) -> int:
    raw = arguments.get("limit", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 8))


def _uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


class SearchConversationTool:
    name = "search_conversation"

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "")
        conv = _uuid(arguments.get("conversation_id"))
        results = self._retriever.search_conversation(
            wax_id, query, limit=_limit(arguments), conversation_id=conv
        )
        return ToolResult(name=self.name, results=results)


class SearchNotebookTool:
    name = "search_notebook"

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "")
        results = self._retriever.search_notebook(
            wax_id, query, limit=_limit(arguments)
        )
        return ToolResult(name=self.name, results=results)


class SearchWorkspaceTool:
    name = "search_workspace"

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "")
        results = self._retriever.search_workspace(
            wax_id, query, limit=_limit(arguments)
        )
        return ToolResult(name=self.name, results=results)


class SearchSemanticallyTool:
    name = "search_semantically"

    def __init__(
        self,
        retriever: HybridRetriever,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self._retriever = retriever
        self._embedder = embedder

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "")
        if self._embedder is None:
            # Degrade to lexical conversation search; never claim semantic success.
            results = self._retriever.search_conversation(
                wax_id, query, limit=_limit(arguments)
            )
            return ToolResult(
                name=self.name,
                results=results,
                metadata={"degraded": True, "reason": "no_embedding_provider"},
            )
        vector = await self._embedder.embed(query)
        results = self._retriever.search_semantically(
            wax_id, vector, limit=_limit(arguments)
        )
        return ToolResult(name=self.name, results=results)


class FollowRelationshipsTool:
    name = "follow_relationships"

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult:
        node_id = _uuid(arguments.get("node_id"))
        if node_id is None:
            return ToolResult(name=self.name, results=())
        direction = str(arguments.get("direction") or "both")
        if direction not in ("outgoing", "incoming", "both"):
            direction = "both"
        results = self._retriever.follow_relationships(
            wax_id, node_id, direction=direction, limit=_limit(arguments)
        )
        return ToolResult(name=self.name, results=results)


class InspectEvidenceTool:
    name = "inspect_evidence"

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult:
        evidence_id = _uuid(arguments.get("evidence_id"))
        if evidence_id is None:
            return ToolResult(name=self.name, results=())
        results = self._retriever.inspect_evidence(wax_id, evidence_id)
        return ToolResult(name=self.name, results=results)


class CheckHistoryTool:
    name = "check_history"

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def execute(self, wax_id: WaxId, arguments: dict[str, Any]) -> ToolResult:
        node_id = _uuid(arguments.get("node_id"))
        if node_id is None:
            return ToolResult(name=self.name, results=())
        # Read-only: return node + attached evidence; no mutation.
        node_hits = self._retriever.inspect_evidence(wax_id, node_id)
        return ToolResult(name=self.name, results=node_hits)


def build_default_tools(
    retriever: HybridRetriever,
    embedder: EmbeddingProvider | None = None,
) -> dict[str, object]:
    tools = [
        SearchConversationTool(retriever),
        SearchNotebookTool(retriever),
        SearchWorkspaceTool(retriever),
        SearchSemanticallyTool(retriever, embedder),
        FollowRelationshipsTool(retriever),
        InspectEvidenceTool(retriever),
        CheckHistoryTool(retriever),
    ]
    return {t.name: t for t in tools}
