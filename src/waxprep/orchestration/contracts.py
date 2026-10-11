"""Teacher-first orchestration contracts.

The architecture is:

    Student message
        |
        v
    Teacher (drives the turn)
        |
        |   may consult Context Intelligence
        v
    Context Intelligence investigates the student's world
        |
        v
    Teacher receives evidence/context
        |
        |   may request another bounded investigation
        v
    Teacher eventually produces the response
        |
        v
    Tutor response is durably saved

The exact student message is the Teacher's primary input. CI may
receive the original text as part of an investigation request, but CI's
synthesis never replaces the original text. CI is a research/investigation
system; it never directly answers the student's educational question.

The Teacher's teaching intelligence is **intentionally unimplemented**
in this build. The Teacher exists as a Protocol; a deterministic mock is
provided for tests. The future Teacher-intelligence build will plug in
a real Teacher that follows the same contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from waxprep.context.models import Evidence
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import HistoryEntry, ProvenanceLink


@dataclass(frozen=True, slots=True)
class ConsultationRequest:
    """A Teacher's request to consult Context Intelligence.

    Attributes:
        wax_id: The student's identifier (for ownership scoping).
        original_message: The exact, untouched student message. CI may
            read it as part of the investigation but must never mutate it.
        teacher_question: What the Teacher wants CI to investigate. This
            is free-form text chosen by the Teacher — CI does not route
            on keywords in it.
        now: The current time. CI uses this for temporal reasoning.
        prior_results: Results from earlier consultations in the same
            turn. Lets the Teacher ask a follow-up investigation and
            lets CI see what it already returned.
        metadata: Bounded, non-sensitive orchestration metadata.
    """

    wax_id: WaxId
    original_message: HistoryEntry
    teacher_question: str
    now: datetime
    prior_results: tuple[ConsultationResult, ...] = ()
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


@dataclass(frozen=True, slots=True)
class ConsultationResult:
    """What Context Intelligence returns to the Teacher.

    CI never directly answers the student's educational question. It
    returns evidence, a synthesis of what was found, uncertainty, and
    an explicit ``insufficient`` flag when the available context does
    not fully answer the Teacher's investigation request.
    """

    evidence: tuple[Evidence, ...]
    synthesis: str
    uncertainty: tuple[str, ...] = ()
    provenance_refs: tuple[ProvenanceLink, ...] = ()
    currentness: dict[UUID, str] = field(
        default_factory=lambda: cast(dict[UUID, str], {})
    )
    insufficient: bool = False
    stopped_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


class TeacherConsultation(Protocol):
    """The capability the Teacher uses to consult Context Intelligence.

    This is a *capability*, not a gatekeeper. The Teacher decides whether
    to consult, what to ask, and whether the result is sufficient. CI
    executes the investigation and returns evidence; it does not decide
    whether the Teacher may proceed.
    """

    async def consult(self, request: ConsultationRequest) -> ConsultationResult: ...


@dataclass(frozen=True, slots=True)
class TeacherInput:
    """Everything the Teacher receives on each round of a turn.

    The original message is the exact student text — never rewritten,
    summarized, or replaced by CI's interpretation.
    """

    wax_id: WaxId
    original_message: HistoryEntry
    now: datetime
    consultation: TeacherConsultation
    prior_results: tuple[ConsultationResult, ...] = ()
    round_number: int = 0
    max_rounds: int = 3


@dataclass(frozen=True, slots=True)
class TeacherOutcome:
    """What the Teacher returns at the end of a round.

    The Teacher signals ``finished=True`` with a ``reply_text`` to end
    the turn. If ``finished=False`` and ``next_question`` is set, the
    orchestrator runs another bounded consultation round and calls the
    Teacher again with the result. If ``finished=False`` and
    ``next_question`` is None, the Teacher has declined to respond
    (the orchestrator treats this as a turn without a tutor reply —
    rare, but allowed when the Teacher has nothing to say).
    """

    reply_text: str | None
    next_question: str | None
    context_used: tuple[ConsultationResult, ...]
    finished: bool


class Teacher(Protocol):
    """The future Teacher.

    Teaching intelligence is **intentionally unimplemented** in this
    build. The Teacher is a Protocol; a deterministic mock is provided
    in ``orchestation.mock_teacher`` for tests. The future
    Teacher-intelligence build will implement this Protocol with real
    teaching logic.

    The Teacher:
    - Receives the exact student message (``original_message``).
    - Decides whether it needs to consult CI.
    - May ask CI a targeted question via ``consultation.consult(...)``.
    - May request another bounded investigation.
    - Eventually returns a reply.
    """

    async def teach(self, inp: TeacherInput) -> TeacherOutcome: ...


__all__ = [
    "ConsultationRequest",
    "ConsultationResult",
    "Teacher",
    "TeacherConsultation",
    "TeacherInput",
    "TeacherOutcome",
]
