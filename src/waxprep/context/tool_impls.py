"""Storage-backed Context Intelligence tools (read-only, WAX-ID scoped)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from waxprep.context.evidence import hydrate
from waxprep.context.proposals import EvidenceRef, Proposal
from waxprep.context.retrieval import ALL_INDEXED, Retriever
from waxprep.context.tools import ToolInvocation, ToolResult
from waxprep.domain.models import ObjectType
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.storage.protocol import Storage

if TYPE_CHECKING:
    from waxprep.context.evolution import EvolutionResult, EvolutionService

MAX_RESULTS = 8


def _limit(arguments: dict[str, Any], default: int = MAX_RESULTS) -> int:
    try:
        value = int(arguments.get("limit", default))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_RESULTS))


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _types(value: object) -> tuple[ObjectType, ...]:
    if not isinstance(value, list):
        return ALL_INDEXED
    chosen: list[ObjectType] = []
    for item in value:  # pyright: ignore[reportUnknownVariableType]
        try:
            kind = ObjectType(str(item))  # pyright: ignore[reportUnknownArgumentType]
        except ValueError:
            continue
        if kind in ALL_INDEXED:
            chosen.append(kind)
    return tuple(chosen) or ALL_INDEXED


def _type_or_none(value: object) -> ObjectType | None:
    try:
        return ObjectType(str(value)) if value else None
    except ValueError:
        return None


class FindExactTool:
    name = "find_exact"

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        results = self._retriever.find_exact(
            invocation.wax_id,
            str(a.get("query") or ""),
            now=invocation.now,
            object_types=_types(a.get("object_types")),
            since=_when(a.get("since")),
            until=_when(a.get("until")),
            limit=_limit(a),
            ignore_recency=bool(a.get("ignore_recency")),
        )
        return ToolResult(name=self.name, results=results)


class FindByMeaningTool:
    name = "find_by_meaning"

    def __init__(
        self, retriever: Retriever, embedder: EmbeddingProvider | None
    ) -> None:
        self._retriever = retriever
        self._embedder = embedder

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        query = str(a.get("query") or "")
        if self._embedder is None or not query.strip():
            # Never pretend meaning-search ran: fall back to words and say so.
            fallback = self._retriever.find_exact(
                invocation.wax_id, query, now=invocation.now, limit=_limit(a)
            )
            return ToolResult(
                name=self.name,
                results=fallback,
                metadata={"degraded": True, "reason": "no_embedding_provider"},
            )
        vector = await self._embedder.embed(query)
        results = self._retriever.find_by_meaning(
            invocation.wax_id,
            vector,
            now=invocation.now,
            object_types=_types(a.get("object_types")),
            since=_when(a.get("since")),
            until=_when(a.get("until")),
            limit=_limit(a),
            ignore_recency=bool(a.get("ignore_recency")),
        )
        return ToolResult(name=self.name, results=results)


class BrowseTimeWindowTool:
    name = "browse_time_window"

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        results = self._retriever.browse_window(
            invocation.wax_id,
            since=_when(a.get("since")),
            until=_when(a.get("until")),
            limit=_limit(a),
            newest_first=bool(a.get("newest_first", True)),
        )
        return ToolResult(name=self.name, results=results)


class ReadAroundTool:
    name = "read_around"

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        entry_id = _uuid(a.get("entry_id")) or invocation.trigger_entry_id
        if entry_id is None:
            return ToolResult(name=self.name, results=())
        results = self._retriever.read_around(
            invocation.wax_id,
            entry_id,
            before=int(a.get("before", 4) or 0),
            after=int(a.get("after", 2) or 0),
        )
        return ToolResult(name=self.name, results=results)


class InspectItemTool:
    name = "inspect_item"

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        item_id = _uuid(a.get("item_id"))
        if item_id is None:
            return ToolResult(name=self.name, results=())
        results = self._retriever.inspect(
            invocation.wax_id, item_id, _type_or_none(a.get("item_type"))
        )
        return ToolResult(name=self.name, results=results)


class TraceSourcesTool:
    name = "trace_sources"

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        item_id = _uuid(a.get("item_id"))
        item_type = _type_or_none(a.get("item_type"))
        if item_id is None or item_type is None:
            return ToolResult(name=self.name, results=())
        return ToolResult(
            name=self.name,
            results=self._retriever.trace_sources(
                invocation.wax_id, item_id, item_type
            ),
        )


class CheckCurrencyTool:
    name = "check_currency"

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        item_id = _uuid(a.get("item_id"))
        item_type = _type_or_none(a.get("item_type"))
        if item_id is None or item_type is None:
            return ToolResult(name=self.name, results=())
        return ToolResult(
            name=self.name,
            results=self._retriever.check_currency(
                invocation.wax_id, item_id, item_type
            ),
        )


class FollowRelationshipsTool:
    name = "follow_relationships"

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        node_id = _uuid(a.get("node_id"))
        if node_id is None:
            return ToolResult(name=self.name, results=())
        direction = str(a.get("direction") or "both")
        if direction not in ("outgoing", "incoming", "both"):
            direction = "both"
        return ToolResult(
            name=self.name,
            results=self._retriever.follow_relationships(
                invocation.wax_id, node_id, direction=direction, limit=_limit(a)
            ),
        )


class ReadArtifactTool:
    name = "read_artifact"

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        version_id = _uuid(invocation.arguments.get("version_id"))
        if version_id is None:
            return ToolResult(name=self.name, results=())
        wax_id = invocation.wax_id
        evidence = []
        version = hydrate(
            self._storage, wax_id, ObjectType.ARTIFACT_VERSION, version_id, "artifact"
        )
        if version is not None:
            evidence.append(version)
        for content in self._storage.list_artifact_content(wax_id, version_id):
            item = hydrate(
                self._storage,
                wax_id,
                ObjectType.ARTIFACT_CONTENT,
                content.id,
                "artifact contents",
            )
            if item is not None:
                evidence.append(item)
        return ToolResult(name=self.name, results=tuple(evidence))


class ProposeLifecycleChangeTool:
    """Write-side proposal tool: the model proposes, the application validates.

    The model invokes this tool with an operation name, a payload, and a
    list of evidence references. The application:

    1. Validates the arguments structurally (ProposalValidator).
    2. Checks that every cited evidence reference exists and belongs to
       the same student (EvolutionService._first_missing_evidence).
    3. Applies the proposal in a single storage transaction
       (EvolutionService.apply) so derived-object + provenance + index
       writes commit atomically — a crash mid-proposal rolls back the
       whole thing.

    The tool returns no Evidence — it is a write tool, not a read tool.
    The result metadata carries the proposal's outcome (accepted/rejected,
    reason, ids of created/invalidated objects). The investigator records
    this metadata in the `investigated` audit trail so the Teacher and
    any operator can see what CI proposed and what the application did.

    Authorization (Blueprint §30):
    - Every cited evidence object is loaded via the student-scoped
      ``hydrate`` helper, which uses ``storage.get_*`` methods that
      enforce Row-Level Security on wax_id. A model cannot cite
      another student's evidence.
    - The proposal's ``wax_id`` comes from the ToolInvocation, not
      from the model's arguments — the model cannot forge a different
      student's identity.
    - The application's ProposalValidator enforces structural rules
      (text length, payload size, allowed operations). The closed
      ``Operation`` enum means the model cannot invent new operations.
    """

    name = "propose_lifecycle_change"

    def __init__(self, evolution: EvolutionService) -> None:
        self._evolution = evolution

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        a = invocation.arguments
        operation = str(a.get("operation") or "")
        raw_payload = a.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        raw_refs_obj = a.get("evidence_refs")
        raw_refs: list[object] = (
            list(raw_refs_obj) if isinstance(raw_refs_obj, list) else []
        )
        reason = str(a.get("reason") or "")[:2000]

        # Map the model's free-form evidence_refs to typed EvidenceRef.
        # The model cannot invent object types — only known ObjectType
        # values are accepted. Unknown types are filtered out.
        evidence_refs: list[EvidenceRef] = []
        for raw in raw_refs:
            if not isinstance(raw, dict):
                continue
            ot = _type_or_none(raw.get("object_type"))
            oid = _uuid(raw.get("object_id"))
            if ot is None or oid is None:
                continue
            quote = raw.get("quote")
            quote_str = str(quote) if quote is not None else None
            evidence_refs.append(EvidenceRef(ot, oid, quote_str))

        proposal = Proposal(
            wax_id=invocation.wax_id,
            operation=operation,
            payload=payload,
            evidence=tuple(evidence_refs),
            reason=reason,
        )
        # The application validates and applies. EvolutionService.apply
        # runs everything in one storage transaction; a failure rolls
        # back. The result is what the application actually did — the
        # model's claim of an operation is not sufficient.
        result: EvolutionResult = self._evolution.apply(proposal, now=invocation.now)
        return ToolResult(
            name=self.name,
            results=(),
            metadata={
                "accepted": result.accepted,
                "reason": result.reason,
                "operation": operation,
                "created_node_id": (
                    str(result.created_node_id) if result.created_node_id else None
                ),
                "created_edge_id": (
                    str(result.created_edge_id) if result.created_edge_id else None
                ),
                "superseded_id": (
                    str(result.superseded_id) if result.superseded_id else None
                ),
                "created_notebook_entry_id": (
                    str(result.created_notebook_entry_id)
                    if result.created_notebook_entry_id
                    else None
                ),
                "created_summary_id": (
                    str(result.created_summary_id)
                    if result.created_summary_id
                    else None
                ),
                "deletion_request_id": (
                    str(result.deletion_request_id)
                    if result.deletion_request_id
                    else None
                ),
                "invalidated_count": len(result.invalidated_ids),
                "tokens_dropped": result.tokens_dropped,
                "embeddings_dropped": result.embeddings_dropped,
            },
        )


def build_default_tools(
    storage: Storage,
    retriever: Retriever,
    embedder: EmbeddingProvider | None = None,
    *,
    evolution: EvolutionService | None = None,
) -> dict[str, Any]:
    tools: list[Any] = [
        FindExactTool(retriever),
        FindByMeaningTool(retriever, embedder),
        BrowseTimeWindowTool(retriever),
        ReadAroundTool(retriever),
        InspectItemTool(retriever),
        TraceSourcesTool(retriever),
        CheckCurrencyTool(retriever),
        FollowRelationshipsTool(retriever),
        ReadArtifactTool(storage),
    ]
    if evolution is not None:
        tools.append(ProposeLifecycleChangeTool(evolution))
    return {t.name: t for t in tools}
