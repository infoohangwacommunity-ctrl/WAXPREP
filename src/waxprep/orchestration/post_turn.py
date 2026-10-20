"""Post-response review after every successful Teacher reply.

Preserves the interaction in history (already done by the turn path).
Records structured quality outcomes as domain events.
Offers CI a stewardship decision so notebook may change when justified.
Does not retrain the model or rewrite system instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.models import InvestigationRequest, InvestigationLimits
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import Event, HistoryEntry, new_id
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PostTurnQualityRecord:
    """Structured, non-secret quality signal for one completed turn."""

    wax_id: WaxId
    student_entry_id: UUID
    tutor_entry_id: UUID
    investigation_status: str
    evidence_count: int
    invalid_selection: bool
    notebook_proposal_accepted: bool
    reply_chars: int
    notes: tuple[str, ...] = ()


class PostTurnReview:
    """Deterministic quality event + optional CI notebook stewardship."""

    def __init__(
        self,
        storage: Storage,
        investigator: ContextInvestigator | None = None,
        *,
        enable_stewardship: bool = True,
    ) -> None:
        self._storage = storage
        self._investigator = investigator
        self._enable_stewardship = enable_stewardship

    def record_quality(self, record: PostTurnQualityRecord, *, now: datetime) -> None:
        """Append a durable quality event (no student plaintext body)."""
        try:
            self._storage.append_event(
                Event(
                    id=new_id(),
                    wax_id=record.wax_id,
                    kind="turn_quality",
                    object_type="history",
                    object_id=record.tutor_entry_id,
                    created_at=now,
                    metadata={
                        "student_entry_id": str(record.student_entry_id),
                        "tutor_entry_id": str(record.tutor_entry_id),
                        "investigation_status": record.investigation_status,
                        "evidence_count": record.evidence_count,
                        "invalid_selection": record.invalid_selection,
                        "notebook_proposal_accepted": record.notebook_proposal_accepted,
                        "reply_chars": record.reply_chars,
                        "notes": list(record.notes)[:8],
                    },
                )
            )
        except Exception:
            logger.exception("failed to persist turn_quality event")

    async def maybe_steward_notebook(
        self,
        *,
        wax_id: WaxId,
        student_entry: HistoryEntry,
        tutor_entry: HistoryEntry,
        now: datetime,
        already_accepted_proposal: bool,
    ) -> list[str]:
        """If no notebook proposal ran during the turn, ask CI once more.

        The model still decides; the application validates. Casual turns
        may simply stop without a write.
        """
        if already_accepted_proposal or not self._enable_stewardship:
            return []
        if self._investigator is None:
            return []
        text = (student_entry.text or "").strip()
        if not text:
            return []
        # Focused stewardship query — no topic hardcoding.
        query = (
            f"Post-turn review. Student said: {text}\n"
            f"Tutor replied ({len(tutor_entry.text or '')} chars).\n"
            f"[trigger_history_entry_id={student_entry.id}]\n"
            "If this exchange established durable understanding useful for "
            "future personalization, and you can cite evidence, call "
            "propose_lifecycle_change. Otherwise stop. Do not invent ids."
        )
        try:
            package = await self._investigator.investigate(
                InvestigationRequest(
                    wax_id=wax_id,
                    query=query,
                    now=now,
                    trigger_entry_id=student_entry.id,
                    limits=InvestigationLimits(
                        max_steps=4,
                        max_tool_calls=4,
                        max_evidence=8,
                    ),
                )
            )
            return list(package.investigated)
        except Exception:
            logger.exception("post-turn stewardship investigation failed")
            return ["post_turn_stewardship_failed"]
