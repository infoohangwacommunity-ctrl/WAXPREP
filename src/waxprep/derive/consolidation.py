"""Consolidating old history into summaries and durable understanding.

Consolidation never deletes or edits history. A summary is a faster way to
find the evidence, always linked back to every entry it covers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from waxprep.clock import Clock
from waxprep.context.evolution import EvolutionService
from waxprep.context.proposals import EvidenceRef, Operation, Proposal
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    HistoryEntry,
    ObjectType,
    ProvenanceLink,
    Summary,
    new_id,
)
from waxprep.index.indexer import Indexer
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
)
from waxprep.storage.protocol import Storage


class ConsolidationError(RuntimeError):
    """The model could not produce a usable consolidation. History is untouched."""


@dataclass(frozen=True, slots=True)
class NotebookDraft:
    payload: dict[str, Any]
    supporting_entry_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ConsolidationOutput:
    summary_text: str
    notebook_drafts: tuple[NotebookDraft, ...] = ()


class ConsolidationModel(Protocol):
    @property
    def name(self) -> str: ...

    async def consolidate(
        self, wax_id: WaxId, entries: tuple[HistoryEntry, ...], now: datetime
    ) -> ConsolidationOutput: ...


def render_entry(entry: HistoryEntry) -> str:
    body = entry.text if entry.text else f"<{entry.content_kind} with no text>"
    return f"[{entry.id}] {entry.occurred_at.isoformat()} {entry.author_kind.value}: {body}"


class ConsolidationService:
    """Turns a batch of older, not-yet-summarised history into a summary."""

    def __init__(
        self,
        storage: Storage,
        evolution: EvolutionService,
        indexer: Indexer,
        model: ConsolidationModel,
        clock: Clock,
    ) -> None:
        self._storage = storage
        self._evolution = evolution
        self._indexer = indexer
        self._model = model
        self._clock = clock

    async def consolidate_student(
        self, wax_id: WaxId, *, older_than: timedelta, max_entries: int = 200
    ) -> Summary | None:
        now = self._clock.now()
        entries = self._storage.list_unsummarized_history(
            wax_id, before=now - older_than, limit=max_entries
        )
        if not entries:
            return None
        output = await self._model.consolidate(wax_id, entries, now)
        if not output.summary_text.strip():
            raise ConsolidationError("model returned an empty summary")

        summary = Summary(
            id=new_id(),
            wax_id=wax_id,
            text=output.summary_text.strip(),
            covers_from=min(e.occurred_at for e in entries),
            covers_until=max(e.occurred_at for e in entries),
            level=1,
            produced_by=self._model.name,
            created_at=now,
        )
        # Links first, summary second: entries only count as covered once the
        # summary row exists, so a failure in between loses nothing.
        self._storage.add_provenance_links(
            tuple(
                ProvenanceLink(
                    id=new_id(),
                    wax_id=wax_id,
                    derived_type=ObjectType.SUMMARY,
                    derived_id=summary.id,
                    source_type=ObjectType.HISTORY,
                    source_id=e.id,
                    created_at=now,
                    source_author_kind=e.author_kind,
                )
                for e in entries
            )
        )
        self._storage.add_summary(summary)
        self._indexer.index(
            wax_id, ObjectType.SUMMARY, summary.id, summary.text, summary.covers_until
        )

        batch_ids = {e.id for e in entries}
        for draft in output.notebook_drafts:
            supporting = tuple(i for i in draft.supporting_entry_ids if i in batch_ids)
            if not supporting or not draft.payload:
                continue  # never keep understanding that cites nothing real
            self._evolution.apply(
                Proposal(
                    wax_id=wax_id,
                    operation=Operation.ADD_NOTEBOOK_ENTRY.value,
                    payload={"payload": draft.payload},
                    evidence=tuple(
                        EvidenceRef(ObjectType.HISTORY, i) for i in supporting
                    ),
                    reason="consolidation of older history",
                ),
                now=now,
            )
        return summary


CONSOLIDATION_INSTRUCTIONS = """
You consolidate part of one student's history for a tutoring system.
The history is never deleted; your summary is only a faster way to find it.

Write a faithful summary. Keep dates and who said what. Keep uncertainty.
Never invent facts. Statements made by the tutor are not facts about the
student. Do not force the content into fixed categories.

Also list durable understanding worth keeping readily available, if any.
Each item must name the entry ids that support it.

Return only JSON:
{"summary": "...",
 "notebook": [{"payload": {...open-ended...}, "supported_by": ["<entry id>", ...]}]}
""".strip()


class GatewayConsolidationModel:
    """Consolidation through the provider-neutral Model Gateway."""

    def __init__(
        self, gateway: ModelGateway, *, model_name: str = "consolidation"
    ) -> None:
        self._gateway = gateway
        self._model_name = model_name

    @property
    def name(self) -> str:
        return self._model_name

    async def consolidate(
        self, wax_id: WaxId, entries: tuple[HistoryEntry, ...], now: datetime
    ) -> ConsolidationOutput:
        listing = "\n".join(render_entry(e) for e in entries)
        response = await self._gateway.complete(
            ModelRequest(
                wax_id=wax_id,
                role=ModelRole.CONSOLIDATION,
                model=self._model_name,
                instructions=CONSOLIDATION_INSTRUCTIONS,
                input=(
                    ContentItem(
                        type=ContentType.TEXT,
                        text=f"Current time (UTC): {now.isoformat()}\nHistory:\n{listing}",
                    ),
                ),
                max_output_tokens=4000,
            )
        )
        return parse_consolidation(response.text)


def parse_consolidation(text: str) -> ConsolidationOutput:
    data = _json_object(text)
    if data is None:
        raise ConsolidationError("model output was not valid JSON")
    summary = data.get("summary")
    if not isinstance(summary, str):
        raise ConsolidationError("model output had no summary")
    drafts: list[NotebookDraft] = []
    raw = data.get("notebook")
    if isinstance(raw, list):
        for item in raw:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item, dict):
                continue
            payload = item.get("payload")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            supported = item.get("supported_by")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if not isinstance(payload, dict) or not isinstance(supported, list):
                continue
            ids: list[UUID] = []
            for value in supported:  # pyright: ignore[reportUnknownVariableType]
                try:
                    ids.append(UUID(str(value)))  # pyright: ignore[reportUnknownArgumentType]
                except ValueError:
                    continue
            drafts.append(
                NotebookDraft(
                    payload={str(k): v for k, v in payload.items()},  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                    supporting_entry_ids=tuple(ids),
                )
            )
    return ConsolidationOutput(summary_text=summary, notebook_drafts=tuple(drafts))


def _json_object(text: str) -> dict[str, Any] | None:
    for candidate in (text.strip(), *re.findall(r"\{.*\}", text, flags=re.DOTALL)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data  # pyright: ignore[reportUnknownVariableType]
    return None
