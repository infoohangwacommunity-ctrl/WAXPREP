"""Quality Judge service: trigger, windowing, persistence, dedup.

The judge never blocks a turn. The turn path only *schedules* an
evaluation job once enough completed student->tutor pairs have
accumulated since the last assessment; the background worker runs the
evaluation. The cursor is the last ``tutor_quality_assessed`` event:
its ``object_id`` is the history entry the previous evaluation window
ended on, so no turn is evaluated twice and none is silently skipped.

Persistence follows the existing split:

- The **event** (``tutor_quality_assessed``) is the durable operational
  record: rating, confidence, window size — operational metadata only,
  validated by the event contract. Every assessment ever made stays
  queryable here.
- The **notebook entry** is the compact personalization shortcut: one
  active ``tutor_quality_assessment`` entry, superseded (never deleted)
  by each fresh assessment, with provenance links to the evaluated
  history entries and search tokens so Context Intelligence can find
  it with its existing tools.

Fairness: a low-confidence assessment is logged as an event but never
written to the notebook, so a single uncertain score cannot bias
future tutoring.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from waxprep.clock import Clock
from waxprep.context.evolution import EvolutionService
from waxprep.context.proposals import EvidenceRef, Operation, Proposal
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    AuthorKind,
    DerivationJob,
    Event,
    HistoryEntry,
    JobStatus,
    NotebookEntry,
    ObjectType,
    new_id,
)
from waxprep.judge.models import (
    JudgeConfidence,
    JudgeModel,
    JudgeRequest,
    TutorQualityAssessment,
    parse_assessment,
    trend_label,
)
from waxprep.orchestration.contracts_ci_first import TutorGuidance
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)

JOB_JUDGE_TUTOR_QUALITY = "judge_tutor_quality"
EVENT_KIND_ASSESSED = "tutor_quality_assessed"
EVENT_KIND_SCHEDULED = "tutor_quality_scheduled"
NOTEBOOK_KIND = "tutor_quality_assessment"
EVENT_SOURCE = "quality_judge"
# Provenance is capped: enough to audit which turns were evaluated,
# never so much that provenance rows explode.
MAX_EVIDENCE_REFS = 8
MAX_ENTRY_CHARS_IN_WINDOW = 600


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    """Evaluation trigger and window knobs (all env-configurable)."""

    every_n_turns: int = 8
    min_pairs: int = 2
    window_entries: int = 24
    max_window_chars: int = 6000

    @property
    def enabled(self) -> bool:
        return self.every_n_turns > 0


def judge_config_from_env(environ: Mapping[str, str]) -> JudgeConfig:
    """Read ``WAXPREP_JUDGE_*`` knobs; ``WAXPREP_JUDGE_EVERY_N_TURNS=0``
    disables judging entirely (no jobs, no model calls)."""

    def _int(name: str, default: int) -> int:
        raw = environ.get(name)
        return int(raw) if raw not in (None, "") else default

    return JudgeConfig(
        every_n_turns=_int("WAXPREP_JUDGE_EVERY_N_TURNS", 8),
        min_pairs=_int("WAXPREP_JUDGE_MIN_PAIRS", 2),
        window_entries=_int("WAXPREP_JUDGE_WINDOW_ENTRIES", 24),
        max_window_chars=_int("WAXPREP_JUDGE_MAX_WINDOW_CHARS", 6000),
    )


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """What one judge run did — for logging and tests.

    status:
      - ``written``: assessment persisted (event + notebook entry)
      - ``skipped_low_confidence``: event only; notebook untouched
      - ``notebook_rejected``: proposal rejected; event keeps the rating
      - ``insufficient_window``: too few new pairs; cursor still advances
      - ``already_assessed``: duplicate run after a partial write
    """

    status: str
    model_called: bool
    notebook_entry_id: UUID | None = None
    rating: int | None = None


class QualityJudgeService:
    """Schedules and runs periodic tutor-quality evaluations."""

    def __init__(
        self,
        *,
        storage: Storage,
        evolution: EvolutionService,
        clock: Clock,
        model: JudgeModel,
        config: JudgeConfig | None = None,
    ) -> None:
        self._storage = storage
        self._evolution = evolution
        self._clock = clock
        self._model = model
        self._config = config or JudgeConfig()

    # --- trigger (turn path: cheap, no model) ----------------------------

    def maybe_schedule(self, wax_id: WaxId, *, now: datetime) -> bool:
        """Schedule one evaluation when enough new turns have accumulated.

        Called after every completed turn. Counts completed
        student->tutor pairs since the last assessment cursor and
        enqueues a durable job when the count reaches
        ``every_n_turns``. Grouping is purely temporal (turns after the
        cursor) — never content matching. A scheduling event records
        which window end a job was queued for, so a busy worker never
        causes the same window to pile up duplicate jobs.
        """
        if not self._config.enabled:
            return False
        window = self._recent_window(wax_id)
        if not window:
            return False
        if self._pairs(window) < self._config.every_n_turns:
            return False
        window_end = window[-1].id
        if self._already_scheduled(wax_id, window_end):
            return False
        self._storage.enqueue_job(
            DerivationJob(
                id=new_id(),
                wax_id=wax_id,
                kind=JOB_JUDGE_TUTOR_QUALITY,
                object_type=ObjectType.HISTORY,
                object_id=window_end,
                status=JobStatus.PENDING,
                attempts=0,
                run_after=now,
                created_at=now,
                updated_at=now,
            )
        )
        self._storage.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind=EVENT_KIND_SCHEDULED,
                object_type="history",
                object_id=window_end,
                created_at=now,
                metadata={
                    "source": EVENT_SOURCE,
                    "kind": "tutor_quality",
                    "status": "scheduled",
                    "wax_id": str(wax_id),
                    "object_type": "history",
                },
            )
        )
        return True

    def current_tutor_guidance(self, wax_id: WaxId) -> TutorGuidance | None:
        """The newest accepted tutor guidance for this student's tutor.

        Reads the active assessment notebook entry directly — the same
        compact encrypted record CI retrieves — so the current accepted
        guidance reaches the Teacher on every turn, not only when CI
        happens to select the entry. Fairness holds by construction:
        low-confidence and rejected assessments never enter the
        notebook, so they can never become guidance. The event stream
        stays operational-only (no model text in event metadata).
        Never raises: guidance is advisory and must never fail a turn.
        """
        try:
            notebook = self._storage.get_notebook(wax_id)
            if notebook is None:
                return None
            for entry in reversed(
                self._storage.list_notebook_entries(wax_id, notebook.id)
            ):
                payload = entry.payload
                if payload.get("kind") != NOTEBOOK_KIND:
                    continue
                if entry.status.value != "active":
                    continue
                text = payload.get("tutor_guidance")
                if not isinstance(text, str) or not text.strip():
                    return None
                return TutorGuidance(
                    text=text.strip(),
                    confidence=str(payload.get("confidence") or "unknown"),
                    assessed_at=self._assessed_at(payload, entry.created_at),
                    source=EVENT_SOURCE,
                )
        except Exception:
            logger.warning("tutor guidance lookup failed", exc_info=True)
        return None

    @staticmethod
    def _assessed_at(payload: Mapping[str, Any], fallback: datetime) -> datetime:
        """When the assessment was made (payload ``as_of``), or a fallback."""
        raw = payload.get("as_of")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return fallback
        return fallback

    def _already_scheduled(self, wax_id: WaxId, window_end: UUID) -> bool:
        """True when a job for this exact window end is already queued.

        The scheduled event advances with each new window, so a job
        that exhausts its retries only blocks re-evaluation of that
        exact window; the next new turn produces a new window end and
        scheduling resumes. Assessment of a window clears the guard:
        after the cursor moves, the same window end can never recur.
        """
        scheduled_end: UUID | None = None
        assessed_end: UUID | None = None
        for event in reversed(self._storage.list_events(wax_id)):
            if scheduled_end is None and event.kind == EVENT_KIND_SCHEDULED:
                scheduled_end = event.object_id
            if assessed_end is None and event.kind == EVENT_KIND_ASSESSED:
                assessed_end = event.object_id
            if scheduled_end is not None and assessed_end is not None:
                break
        if scheduled_end is None:
            return False
        return scheduled_end == window_end and assessed_end != window_end

    # --- evaluation (worker path) ----------------------------------------

    async def evaluate_recent(self, wax_id: WaxId) -> JudgeOutcome:
        """Evaluate the turns added since the last assessment.

        Raises on model/contract failure so the job system retries
        with backoff; the cursor only advances on recorded outcomes.
        """
        now = self._clock.now()
        cursor = self._cursor(wax_id)
        window = self._eval_window(wax_id, cursor)
        pairs = self._pairs(window)
        window_end = window[-1].id if window else cursor

        if window:
            duplicate = self._assessment_entry_for_window_end(wax_id, window[-1].id)
            if duplicate is not None:
                # A retry after a partial write: the assessment is
                # already durable; only the cursor event was lost.
                self._record(
                    wax_id,
                    now=now,
                    status="already_assessed",
                    window_end=window_end,
                    rating=None,
                    confidence=None,
                    pairs=pairs,
                )
                return JudgeOutcome("already_assessed", model_called=False)

        if pairs < self._config.min_pairs:
            # Cheap close: no model call, but the cursor still advances
            # so the same thin window is never re-offered.
            self._record(
                wax_id,
                now=now,
                status="insufficient_window",
                window_end=window_end,
                rating=None,
                confidence=None,
                pairs=pairs,
            )
            return JudgeOutcome("insufficient_window", model_called=False)

        previous = self._previous_assessment(wax_id)
        previous_ratings = _rating_history(previous[1]) if previous else ()
        previous_rating = previous_ratings[-1] if previous_ratings else None

        raw = await self._model.evaluate(
            JudgeRequest(
                wax_id=wax_id,
                window_text=self._window_text(window),
                window_first_id=window[0].id,
                window_last_id=window[-1].id,
                window_turns=pairs,
                previous_payload=previous[1] if previous else None,
            )
        )
        assessment = parse_assessment(raw)  # JudgeOutputError -> job retry
        trend = trend_label(assessment.overall, previous_rating)

        entry_id: UUID | None = None
        status = "skipped_low_confidence"
        if assessment.confidence is not JudgeConfidence.LOW:
            entry_id = self._apply_to_notebook(
                wax_id,
                assessment,
                now=now,
                window=window,
                pairs=pairs,
                trend=trend,
                previous_ratings=previous_ratings,
                old_entry_id=previous[0] if previous else None,
            )
            status = "written" if entry_id is not None else "notebook_rejected"

        self._record(
            wax_id,
            now=now,
            status=status,
            window_end=window_end,
            rating=assessment.overall,
            confidence=assessment.confidence.value,
            pairs=pairs,
        )
        return JudgeOutcome(
            status,
            model_called=True,
            notebook_entry_id=entry_id,
            rating=assessment.overall,
        )

    # --- internals -----------------------------------------------------------

    def _cursor(self, wax_id: WaxId) -> UUID | None:
        """The history entry the last assessment window ended on."""
        for event in reversed(self._storage.list_events(wax_id)):
            if event.kind == EVENT_KIND_ASSESSED:
                return event.object_id
        return None

    def _recent_window(self, wax_id: WaxId) -> tuple[HistoryEntry, ...]:
        """History strictly after the cursor, capped to ``window_entries``."""
        newest = self._storage.list_history(
            wax_id,
            limit=max(self._config.window_entries * 4, 128),
            newest_first=True,
        )
        cursor = self._cursor(wax_id)
        if cursor is not None:
            # newest_first listing: everything newer than the cursor
            # sits at indices [0, cut); the cursor itself is at cut.
            cut = next((i for i, e in enumerate(newest) if e.id == cursor), None)
            if cut is not None:
                newest = newest[:cut]
        entries = tuple(reversed(newest))
        return entries[-self._config.window_entries :]

    def _eval_window(
        self, wax_id: WaxId, cursor: UUID | None
    ) -> tuple[HistoryEntry, ...]:
        """Window for one evaluation: recent window, char-capped.

        If the window exceeds ``max_window_chars``, the oldest entries
        are dropped first — the evaluated window is exactly what the
        model saw, and the ids recorded in the payload reflect that.
        """
        window = self._recent_window(wax_id)
        cap = self._config.max_window_chars
        while window and sum(len(e.text or "") for e in window) > cap:
            window = window[1:]
        return window

    @staticmethod
    def _pairs(window: tuple[HistoryEntry, ...]) -> int:
        """Completed student->tutor pairs inside the window."""
        answered = {
            e.reply_to_id
            for e in window
            if e.author_kind is AuthorKind.TUTOR and e.reply_to_id is not None
        }
        return sum(
            1
            for e in window
            if e.author_kind is AuthorKind.STUDENT and e.id in answered
        )

    @staticmethod
    def _window_text(window: tuple[HistoryEntry, ...]) -> str:
        """Render the window for the judge — verbatim, oldest first."""
        lines: list[str] = []
        for entry in window:
            if entry.author_kind is AuthorKind.STUDENT:
                role = "student"
            elif entry.author_kind is AuthorKind.TUTOR:
                role = "tutor"
            else:
                role = "system"
            text = (entry.text or "").strip()
            if len(text) > MAX_ENTRY_CHARS_IN_WINDOW:
                text = text[:MAX_ENTRY_CHARS_IN_WINDOW] + "…"
            lines.append(f"{role}: {text}")
        return "\n".join(lines)

    def _previous_assessment(self, wax_id: WaxId) -> tuple[UUID, dict[str, Any]] | None:
        """The active assessment notebook entry, if one exists."""
        notebook = self._storage.get_notebook(wax_id)
        if notebook is None:
            return None
        for entry in reversed(self._storage.list_notebook_entries(wax_id, notebook.id)):
            if entry.payload.get("kind") == NOTEBOOK_KIND:
                return entry.id, dict(entry.payload)
        return None

    def _assessment_entry_for_window_end(
        self, wax_id: WaxId, window_last_id: UUID
    ) -> NotebookEntry | None:
        """Active assessment that already covers this exact window end."""
        notebook = self._storage.get_notebook(wax_id)
        if notebook is None:
            return None
        for entry in self._storage.list_notebook_entries(wax_id, notebook.id):
            if entry.payload.get("kind") == NOTEBOOK_KIND and entry.payload.get(
                "window_last_id"
            ) == str(window_last_id):
                return entry
        return None

    def _apply_to_notebook(
        self,
        wax_id: WaxId,
        assessment: TutorQualityAssessment,
        *,
        now: datetime,
        window: tuple[HistoryEntry, ...],
        pairs: int,
        trend: str,
        previous_ratings: tuple[int, ...],
        old_entry_id: UUID | None,
    ) -> UUID | None:
        """Write the assessment as a notebook entry (add, then supersede).

        Uses the existing ``EvolutionService`` proposal path so
        validation, provenance linking, transactionality, and search
        indexing all apply unchanged. Returns the new entry id, or
        None when the proposal was rejected (logged, non-fatal).
        """
        payload = assessment.to_notebook_payload(
            as_of=now.isoformat(),
            trend=trend,
            window_turns=pairs,
            window_first_id=window[0].id,
            window_last_id=window[-1].id,
            previous_ratings=previous_ratings + (assessment.overall,),
        )
        operation = (
            Operation.SUPERSEDE_NOTEBOOK_ENTRY
            if old_entry_id is not None
            else Operation.ADD_NOTEBOOK_ENTRY
        )
        outer: dict[str, Any] = {"payload": payload}
        if old_entry_id is not None:
            outer["old_entry_id"] = str(old_entry_id)
        result = self._evolution.apply(
            Proposal(
                wax_id=wax_id,
                operation=operation.value,
                payload=outer,
                evidence=self._evidence_refs(window),
                reason=(
                    f"periodic tutor-quality assessment of {pairs} "
                    f"tutoring pair(s) ending at history entry {window[-1].id}"
                ),
            ),
            now=now,
        )
        if not result.accepted:
            logger.warning(
                "quality judge notebook proposal rejected",
                extra={"reason": result.reason},
            )
            return None
        return result.created_notebook_entry_id

    @staticmethod
    def _evidence_refs(window: tuple[HistoryEntry, ...]) -> tuple[EvidenceRef, ...]:
        """Cap provenance: point at the evaluated turns, evenly spread."""
        if len(window) <= MAX_EVIDENCE_REFS:
            chosen: tuple[HistoryEntry, ...] = window
        else:
            step = len(window) / MAX_EVIDENCE_REFS
            chosen = tuple(window[int(i * step)] for i in range(MAX_EVIDENCE_REFS))
        return tuple(
            EvidenceRef(object_type=ObjectType.HISTORY, object_id=e.id) for e in chosen
        )

    def _record(
        self,
        wax_id: WaxId,
        *,
        now: datetime,
        status: str,
        window_end: UUID | None,
        rating: int | None,
        confidence: str | None,
        pairs: int,
    ) -> None:
        """Append the durable assessed event (the cursor for the next run)."""
        metadata: dict[str, Any] = {
            "source": EVENT_SOURCE,
            "kind": "tutor_quality",
            "status": status,
            "wax_id": str(wax_id),
            "object_type": "history",
            "window_turns": int(pairs),
        }
        if window_end is not None:
            metadata["object_id"] = str(window_end)
        if rating is not None:
            metadata["rating"] = int(rating)
        if confidence is not None:
            metadata["confidence"] = confidence
        self._storage.append_event(
            Event(
                id=new_id(),
                wax_id=wax_id,
                kind=EVENT_KIND_ASSESSED,
                object_type="history",
                object_id=window_end,
                created_at=now,
                metadata=metadata,
            )
        )


def _rating_history(payload: Mapping[str, Any]) -> tuple[int, ...]:
    """Previous ratings carried in a notebook payload (newest last)."""
    raw = payload.get("previous_ratings")
    if not isinstance(raw, list):
        return ()
    out: list[int] = []
    for item in raw:
        if isinstance(item, int) and not isinstance(item, bool):
            out.append(item)
    return tuple(out)


__all__ = [
    "EVENT_KIND_ASSESSED",
    "EVENT_KIND_SCHEDULED",
    "EVENT_SOURCE",
    "JOB_JUDGE_TUTOR_QUALITY",
    "JudgeConfig",
    "JudgeOutcome",
    "NOTEBOOK_KIND",
    "QualityJudgeService",
    "judge_config_from_env",
]
