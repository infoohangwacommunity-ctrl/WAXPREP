"""Founder's vision §6 — current learning state notebook kind.

Pins the ``set_learning_state`` operation and the ``read_learning_state``
retrieval tool. The state is superseded-not-deleted when the lesson
advances; the Teacher can render the active state to resume the
lesson instead of starting from zero.

These tests use the real ``EvolutionService`` over ``InMemoryStorage``
so the full proposal -> validation -> persistence -> retrieval loop
runs end-to-end. No keyword routing, no hardcoded conversation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from waxprep.context.evolution import EvolutionService
from waxprep.context.proposals import (
    EvidenceRef,
    Operation,
    Proposal,
    ProposalValidator,
)
from waxprep.context.tool_impls import (
    ProposeLifecycleChangeTool,
    ReadLearningStateTool,
)
from waxprep.context.tools import ToolInvocation, ToolResult
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    ObjectType,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.security.cipher import AesGcmFieldCipher, KeyRing
from waxprep.storage.memory import InMemoryStorage

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
CIPHER = AesGcmFieldCipher(KeyRing(keys={1: b"t" * 32}, active_key_id=1))


def _world() -> tuple[InMemoryStorage, EvolutionService, Indexer]:
    storage = InMemoryStorage()
    indexer = Indexer(
        storage=storage,
        token_index=TokenIndex(cipher=CIPHER),
        clock=_StaticClock(),
    )
    evolution = EvolutionService(storage=storage, indexer=indexer)
    return storage, evolution, indexer


class _StaticClock:
    def now(self) -> datetime:
        return NOW


def _seed_student(storage: InMemoryStorage) -> tuple[Any, HistoryEntry]:
    wax_id = new_wax_id()
    storage.create_student(
        Student(
            wax_id=wax_id,
            created_at=NOW,
            updated_at=NOW,
            status=StudentStatus.ACTIVE,
        )
    )
    history = HistoryEntry(
        id=new_id(),
        wax_id=wax_id,
        author_kind=AuthorKind.STUDENT,
        content_kind="text",
        text="I think I understand multiplication now.",
        occurred_at=NOW,
        recorded_at=NOW,
    )
    storage.append_history_entry(history)
    return wax_id, history


def _state_payload(*, subject: str = "Mathematics") -> dict[str, Any]:
    return {
        "payload": {
            "subject": subject,
            "topic": "Quadratic equations",
            "current_stage": "Understanding the meaning of x squared",
            "subtopics": [
                {"name": "Unknowns in simple equations", "status": "demonstrated"},
                {"name": "Meaning of powers", "status": "developing"},
                {"name": "Factorisation", "status": "not_assessed"},
                {"name": "Quadratic formula", "status": "not_assessed"},
            ],
            "teaching_observation": (
                "Student understood exponentiation better after seeing "
                "repeated multiplication."
            ),
            "next_sensible_step": (
                "Check whether they can calculate simple squares independently."
            ),
        }
    }


# --- validator -------------------------------------------------------------


def test_validator_accepts_well_formed_learning_state() -> None:
    v = ProposalValidator()
    decision = v.validate(
        Proposal(
            wax_id=new_wax_id(),
            operation=Operation.SET_LEARNING_STATE.value,
            payload=_state_payload(),
            evidence=(
                EvidenceRef(object_type=ObjectType.HISTORY, object_id=UUID(int=1)),
            ),
            reason="lesson advanced",
        )
    )
    assert decision.accepted, decision.reason


def test_validator_pins_kind_to_learning_state() -> None:
    """The validator MUST pin ``kind=learning_state`` so the retrieval
    tool and the renderer can find these entries without a free-text
    convention."""
    v = ProposalValidator()
    payload = _state_payload()
    v.validate(
        Proposal(
            wax_id=new_wax_id(),
            operation=Operation.SET_LEARNING_STATE.value,
            payload=payload,
            evidence=(
                EvidenceRef(object_type=ObjectType.HISTORY, object_id=UUID(int=1)),
            ),
            reason="lesson advanced",
        )
    )
    assert payload["payload"]["kind"] == "learning_state"


def test_validator_rejects_missing_required_fields() -> None:
    v = ProposalValidator()
    base = _state_payload()
    # Drop the subject — must fail.
    base["payload"].pop("subject")
    decision = v.validate(
        Proposal(
            wax_id=new_wax_id(),
            operation=Operation.SET_LEARNING_STATE.value,
            payload=base,
            evidence=(
                EvidenceRef(object_type=ObjectType.HISTORY, object_id=UUID(int=1)),
            ),
            reason="bad",
        )
    )
    assert not decision.accepted
    assert "subject" in decision.reason


def test_validator_rejects_unknown_subtopic_status() -> None:
    v = ProposalValidator()
    base = _state_payload()
    base["payload"]["subtopics"][0]["status"] = "mastered"  # not in allowed set
    decision = v.validate(
        Proposal(
            wax_id=new_wax_id(),
            operation=Operation.SET_LEARNING_STATE.value,
            payload=base,
            evidence=(
                EvidenceRef(object_type=ObjectType.HISTORY, object_id=UUID(int=1)),
            ),
            reason="bad",
        )
    )
    assert not decision.accepted
    assert "subtopic.status" in decision.reason


# --- evolution handler ------------------------------------------------------


def test_set_learning_state_creates_first_state() -> None:
    storage, evolution, _ = _world()
    wax_id, history = _seed_student(storage)
    tool = ProposeLifecycleChangeTool(evolution=evolution)
    result = _run_tool(
        tool,
        wax_id,
        operation=Operation.SET_LEARNING_STATE.value,
        payload=_state_payload(),
        evidence_refs=[
            {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
        ],
    )
    metadata = result.metadata
    assert metadata["accepted"] is True
    new_id_value = metadata["created_notebook_entry_id"]
    assert new_id_value is not None
    # No previous state existed — superseded_id should be None.
    assert metadata["superseded_id"] is None


def test_set_learning_state_supersedes_previous_for_same_subject() -> None:
    """When a previous active ``learning_state`` entry exists for the same
    subject, the new state supersedes it (not deletes it)."""
    storage, evolution, _ = _world()
    wax_id, history = _seed_student(storage)
    tool = ProposeLifecycleChangeTool(evolution=evolution)

    first = _run_tool(
        tool,
        wax_id,
        operation=Operation.SET_LEARNING_STATE.value,
        payload=_state_payload(),
        evidence_refs=[
            {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
        ],
    )
    first_id = UUID(first.metadata["created_notebook_entry_id"])
    # Second state for the SAME subject — must supersede the first.
    second = _run_tool(
        tool,
        wax_id,
        operation=Operation.SET_LEARNING_STATE.value,
        payload=_state_payload(),
        evidence_refs=[
            {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
        ],
    )
    assert second.metadata["accepted"] is True
    second_id = UUID(second.metadata["created_notebook_entry_id"])
    assert second_id != first_id
    assert second.metadata["superseded_id"] == str(first_id)

    # The old entry stays for audit; its status is SUPERSEDED.
    notebook = storage.get_notebook(wax_id)
    assert notebook is not None
    old_entry = storage.get_notebook_entry(wax_id, first_id)
    assert old_entry is not None
    assert old_entry.status.value == "superseded"
    assert old_entry.superseded_by_id == second_id


def test_set_learning_state_keeps_other_subjects_active() -> None:
    """A learning state for one subject does NOT supersede a different
    subject's active state — the key is (subject)."""
    storage, evolution, _ = _world()
    wax_id, history = _seed_student(storage)
    tool = ProposeLifecycleChangeTool(evolution=evolution)
    # State for Mathematics.
    math_result = _run_tool(
        tool,
        wax_id,
        operation=Operation.SET_LEARNING_STATE.value,
        payload=_state_payload(subject="Mathematics"),
        evidence_refs=[
            {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
        ],
    )
    # State for Chemistry — different subject.
    chem_result = _run_tool(
        tool,
        wax_id,
        operation=Operation.SET_LEARNING_STATE.value,
        payload=_state_payload(subject="Chemistry"),
        evidence_refs=[
            {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
        ],
    )
    math_id = UUID(math_result.metadata["created_notebook_entry_id"])
    chem_id = UUID(chem_result.metadata["created_notebook_entry_id"])
    # The Chemistry write must NOT have superseded the Mathematics entry.
    assert chem_result.metadata["superseded_id"] is None
    # Both are still active.
    math_entry = storage.get_notebook_entry(wax_id, math_id)
    chem_entry = storage.get_notebook_entry(wax_id, chem_id)
    assert math_entry is not None
    assert chem_entry is not None
    assert math_entry.status.value == "active"
    assert chem_entry.status.value == "active"


# --- read_learning_state tool ----------------------------------------------


def test_read_learning_state_returns_active_state_only() -> None:
    storage, evolution, _ = _world()
    wax_id, history = _seed_student(storage)
    propose = ProposeLifecycleChangeTool(evolution=evolution)
    _run_tool(
        propose,
        wax_id,
        operation=Operation.SET_LEARNING_STATE.value,
        payload=_state_payload(),
        evidence_refs=[
            {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
        ],
    )
    # Supersede with a new state.
    _run_tool(
        propose,
        wax_id,
        operation=Operation.SET_LEARNING_STATE.value,
        payload=_state_payload(),
        evidence_refs=[
            {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
        ],
    )
    # read_learning_state should return ONLY the active (latest) state.
    read_tool = ReadLearningStateTool(storage=storage)
    result = _run_read(read_tool, wax_id)
    assert result.metadata["notebook_exists"] is True
    assert result.metadata["active_states"] == 1
    assert len(result.results) == 1
    content = result.results[0].content
    # The new state's body carries the subject + topic.
    assert "subject: Mathematics" in content
    assert "topic: Quadratic equations" in content


def test_read_learning_state_subject_filter() -> None:
    storage, evolution, _ = _world()
    wax_id, history = _seed_student(storage)
    propose = ProposeLifecycleChangeTool(evolution=evolution)
    for subject in ("Mathematics", "Chemistry", "Biology"):
        _run_tool(
            propose,
            wax_id,
            operation=Operation.SET_LEARNING_STATE.value,
            payload=_state_payload(subject=subject),
            evidence_refs=[
                {"object_type": ObjectType.HISTORY.value, "object_id": str(history.id)}
            ],
        )
    read_tool = ReadLearningStateTool(storage=storage)
    # Filter to Mathematics only.
    result = _run_read(read_tool, wax_id, subject="Mathematics")
    assert result.metadata["active_states"] == 1
    assert "subject: Mathematics" in result.results[0].content
    # No filter -> every active state.
    all_result = _run_read(read_tool, wax_id)
    assert all_result.metadata["active_states"] == 3


def test_read_learning_state_empty_when_no_states() -> None:
    storage, _, _ = _world()
    wax_id, _ = _seed_student(storage)
    # No notebook yet — the tool reports notebook_exists=False honestly.
    read_tool = ReadLearningStateTool(storage=storage)
    result = _run_read(read_tool, wax_id)
    assert result.metadata["notebook_exists"] is False
    assert result.metadata["active_states"] == 0
    assert result.results == ()


# --- Teacher brief rendering ------------------------------------------------


def test_teacher_brief_renders_uncertainty_section() -> None:
    """Founder's vision §8 (memory truth): when CI's ContextPackage
    carries a non-empty ``uncertainty`` tuple, the Teacher's brief
    MUST surface it as a "What CI is uncertain about" section."""
    from waxprep.context.models import ContextPackage, InvestigationStatus
    from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst

    package = ContextPackage(
        wax_id=new_wax_id(),
        query="resume",
        evidence=(),
        summary="Student is working on quadratic equations.",
        uncertainty=("exchange_dropped:no_reason_given", "short_window"),
        investigation_status=InvestigationStatus.SUCCESSFUL,
    )
    brief = ModelTeacherCiFirst._render_brief(package)
    assert "## What CI is uncertain about" in brief
    assert "exchange_dropped:no_reason_given" in brief
    assert "short_window" in brief


def test_teacher_brief_renders_per_evidence_uncertainty_label() -> None:
    """Per-evidence ``confidence_note`` becomes a trailing
    ``(uncertain: <reason>)`` label so the Teacher can tell apart
    established evidence from inference."""
    from waxprep.context.models import (
        ContextDecision,
        ContextPackage,
        Evidence,
        EvidenceType,
        InvestigationStatus,
    )
    from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst

    wax_id = new_wax_id()
    established = Evidence(
        id=new_id(),
        wax_id=wax_id,
        evidence_type=EvidenceType.HISTORY,
        source_id=new_id(),
        content="Student solved 5 questions correctly.",
        created_at=NOW,
        relevance_reason="demonstrated mastery",
        decision=ContextDecision.INCLUDE,
        confidence_note=None,
    )
    inferred = Evidence(
        id=new_id(),
        wax_id=wax_id,
        evidence_type=EvidenceType.HISTORY,
        source_id=new_id(),
        content="Student claims to understand fractions.",
        created_at=NOW,
        relevance_reason="self-assessment only",
        decision=ContextDecision.INCLUDE_WITH_CAUTION,
        confidence_note="needs_student_confirmation: tutor-only evidence",
    )
    package = ContextPackage(
        wax_id=wax_id,
        query="fractions",
        evidence=(established, inferred),
        summary="",
        investigation_status=InvestigationStatus.SUCCESSFUL,
    )
    rendered = ModelTeacherCiFirst._render_evidence_plain(package)
    assert rendered, "evidence rendering must not be empty"
    assert "(uncertain: needs_student_confirmation" in rendered
    # Established evidence must NOT carry an uncertainty label.
    established_line = next(
        line for line in rendered.splitlines() if "solved 5 questions" in line
    )
    assert "(uncertain:" not in established_line


# --- helpers ----------------------------------------------------------------


def _run_tool(
    tool: ProposeLifecycleChangeTool,
    wax_id: Any,
    *,
    operation: str,
    payload: dict[str, Any],
    evidence_refs: list[dict[str, str]],
) -> ToolResult:
    import asyncio

    invocation = ToolInvocation(
        wax_id=wax_id,
        arguments={
            "operation": operation,
            "payload": payload,
            "evidence_refs": evidence_refs,
            "reason": "lesson advanced",
        },
        now=NOW,
    )
    return asyncio.run(tool.execute(invocation))


def _run_read(
    tool: ReadLearningStateTool,
    wax_id: Any,
    *,
    subject: str | None = None,
) -> ToolResult:
    import asyncio

    arguments: dict[str, Any] = {}
    if subject is not None:
        arguments["subject"] = subject
    invocation = ToolInvocation(wax_id=wax_id, arguments=arguments, now=NOW)
    return asyncio.run(tool.execute(invocation))


# Re-export so type-checkers see the symbol used inside _run_tool's signature.
__all__ = [
    "test_validator_accepts_well_formed_learning_state",
    "test_validator_pins_kind_to_learning_state",
    "test_validator_rejects_missing_required_fields",
    "test_validator_rejects_unknown_subtopic_status",
    "test_set_learning_state_creates_first_state",
    "test_set_learning_state_supersedes_previous_for_same_subject",
    "test_set_learning_state_keeps_other_subjects_active",
    "test_read_learning_state_returns_active_state_only",
    "test_read_learning_state_subject_filter",
    "test_read_learning_state_empty_when_no_states",
    "test_teacher_brief_renders_uncertainty_section",
    "test_teacher_brief_renders_per_evidence_uncertainty_label",
]
