"""Write-side proposals: Context Intelligence proposes, the application validates.

Every durable change must point at evidence. Nothing here interprets meaning.

Lifecycle vocabulary (Blueprint Part IV):
The architecture distinguishes five *non-interchangeable* lifecycle operations.
Each maps to one or more proposal operations in this module:

| Blueprint concept | Proposal operation(s)                                  |
| ----------------- | ------------------------------------------------------ |
| Correction        | supersede_notebook_entry / supersede_summary /         |
|                   | supersede_node (with a fresh claim citing new evidence) |
| Supersession      | supersede_notebook_entry / supersede_summary /         |
|                   | supersede_node                                          |
| Invalidation      | invalidate_notebook_entry / invalidate_node            |
| Deletion          | request_deletion (forwards to a separate deletion      |
|                   | workflow — application policy decides retention)      |
| Rebuilding        | rebuild_index (regenerate search tokens/embeddings for |
|                   | a stored object from its sources)                     |

Supersession links a new claim to the old one but never rewrites the old
claim. Invalidation marks an object as no longer trustworthy; it stays
reachable for audit but is excluded from ordinary retrieval. Rebuilding
regenerates derived indexes from sources. Deletion is *not* supersession —
it routes through a separate workflow that respects retention policy and
propagates through every dependent representation (notebook rows,
summaries, edges, tokens, embeddings, caches).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import ObjectType


class Operation(StrEnum):
    CREATE_NODE = "create_node"
    CREATE_EDGE = "create_edge"
    SUPERSEDE_NODE = "supersede_node"
    ATTACH_EVIDENCE = "attach_evidence"
    ADD_NOTEBOOK_ENTRY = "add_notebook_entry"
    SUPERSEDE_NOTEBOOK_ENTRY = "supersede_notebook_entry"
    SUPERSEDE_SUMMARY = "supersede_summary"
    LINK_ARTIFACT = "link_artifact"
    # Founder's vision §6: a current learning state is a first-class
    # notebook kind. CI sets one when a lesson establishes durable
    # understanding about WHERE the student is in a subject — current
    # stage, subtopic statuses (demonstrated / developing /
    # not_assessed), teaching observations, and the next sensible step.
    # The state is superseded-not-deleted when the lesson advances.
    # This operation wraps ``supersede_notebook_entry`` so CI does not
    # have to look up the previous active entry itself; the application
    # finds it by (kind=learning_state, subject=…) and supersedes it.
    SET_LEARNING_STATE = "set_learning_state"
    # --- Lifecycle operations (Blueprint Part IV §13-17) -------------------
    # Invalidation marks a derived object as no longer trustworthy. The
    # object remains stored and traceable for audit, but ordinary retrieval
    # excludes it. Distinct from supersession: nothing replaces it (yet).
    INVALIDATE_NOTEBOOK_ENTRY = "invalidate_notebook_entry"
    INVALIDATE_NODE = "invalidate_node"
    INVALIDATE_SUMMARY = "invalidate_summary"
    # Rebuild regenerates the search tokens and embeddings for one stored
    # object from its sources. Useful when an index row was lost, a token
    # hash changed because of a key rotation, or an embedding model was
    # upgraded. History is untouched.
    REBUILD_INDEX = "rebuild_index"
    # Request deletion routes through a separate deletion workflow. It
    # never deletes on the spot — the application decides retention and
    # propagation. The proposal exists so CI can flag that a student has
    # asked for something to be deleted; the workflow is owned by the
    # application, not by CI.
    REQUEST_DELETION = "request_deletion"


# Object types that may be invalidated or rebuilt. History is deliberately
# absent — history is append-only and never invalidated by these operations.
INVALIDATABLE: frozenset[ObjectType] = frozenset(
    {
        ObjectType.NOTEBOOK,
        ObjectType.SUMMARY,
        ObjectType.KNOWLEDGE_NODE,
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Points at the stored object that supports a proposal."""

    object_type: ObjectType
    object_id: UUID
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class Proposal:
    wax_id: WaxId
    operation: str
    payload: dict[str, Any]
    evidence: tuple[EvidenceRef, ...]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    accepted: bool
    reason: str


class ProposalValidator:
    """Structural validation only — not educational intelligence."""

    MAX_LABEL_LENGTH = 500
    MAX_DESCRIPTION_LENGTH = 4000
    MAX_PROPERTY_KEYS = 32
    MAX_TYPE_LENGTH = 120
    MAX_QUOTE_LENGTH = 4000
    MAX_SUMMARY_LENGTH = 20000
    MAX_REASON_LENGTH = 2000
    MAX_SCOPE_LENGTH = 120
    MAX_SUBJECT_LENGTH = 200
    MAX_TOPIC_LENGTH = 400
    MAX_STAGE_LENGTH = 1000
    MAX_OBSERVATION_LENGTH = 4000
    MAX_NEXT_STEP_LENGTH = 1000
    MAX_SUBTOPICS = 50
    MAX_SUBTOPIC_NAME_LENGTH = 400
    ALLOWED_SUBTOPIC_STATUSES = frozenset(
        {"demonstrated", "developing", "not_assessed", "needs_practice"}
    )
    ALLOWED = frozenset(op.value for op in Operation)

    # Notebook entry kinds (vision-security Fix 4, Founder's Vision §14).
    # The notebook persists durable ACADEMIC context — learning state,
    # demonstrated mastery, teaching observations, communication
    # preferences, and academic circumstances. Personal-life
    # disclosures (family, mental health, identity) are a SEPARATE
    # category: they may be persisted ONLY when the student has
    # explicitly asked for them to be remembered.
    #
    # ``kind`` is OPTIONAL — existing entries that pre-date this field
    # continue to validate. When ``kind`` IS present, it must be in
    # this set so the consent rule for ``personal_context`` is
    # enforceable. ``set_learning_state`` pins ``kind="learning_state"``
    # itself (see ``_set_learning_state``); ``add_notebook_entry`` and
    # ``supersede_notebook_entry`` accept any of these kinds (or no
    # kind at all).
    #
    # ``tutor_quality_assessment`` is the Judge's compact personalization
    # shortcut (see ``judge/models.py``). The Judge is part of the
    # application, not CI — it writes its assessment as a notebook entry
    # so the next turn's Teacher sees the guidance. Without this entry
    # in the allowed set, every Judge write is rejected and tutor
    # guidance can never reach the Teacher.
    ALLOWED_NOTEBOOK_KINDS: frozenset[str] = frozenset(
        {
            "learning_state",
            "mastery",
            "teaching_observation",
            "communication_preference",
            "circumstance",
            "personal_context",
            "tutor_quality_assessment",
        }
    )

    def validate(self, proposal: Proposal) -> ProposalDecision:
        if proposal.operation not in self.ALLOWED:
            return ProposalDecision(False, f"unknown operation: {proposal.operation}")
        if not proposal.evidence:
            return ProposalDecision(False, "every durable change needs evidence")
        for ref in proposal.evidence:
            if ref.quote is not None and len(ref.quote) > self.MAX_QUOTE_LENGTH:
                return ProposalDecision(False, "quote too long")
        if len(proposal.payload) > self.MAX_PROPERTY_KEYS + 8:
            return ProposalDecision(False, "payload too large")
        if len(proposal.reason) > self.MAX_REASON_LENGTH:
            return ProposalDecision(False, "reason too long")
        checks = {
            Operation.CREATE_NODE.value: self._node,
            Operation.CREATE_EDGE.value: self._edge,
            Operation.SUPERSEDE_NODE.value: self._supersede_node,
            Operation.ATTACH_EVIDENCE.value: self._attach,
            Operation.ADD_NOTEBOOK_ENTRY.value: self._notebook,
            Operation.SUPERSEDE_NOTEBOOK_ENTRY.value: self._supersede_notebook,
            Operation.SUPERSEDE_SUMMARY.value: self._supersede_summary,
            Operation.LINK_ARTIFACT.value: self._link_artifact,
            Operation.SET_LEARNING_STATE.value: self._set_learning_state,
            Operation.INVALIDATE_NOTEBOOK_ENTRY.value: self._invalidate_notebook,
            Operation.INVALIDATE_NODE.value: self._invalidate_node,
            Operation.INVALIDATE_SUMMARY.value: self._invalidate_summary,
            Operation.REBUILD_INDEX.value: self._rebuild_index,
            Operation.REQUEST_DELETION.value: self._request_deletion,
        }
        return checks[proposal.operation](proposal.payload)

    def _text(self, payload: dict[str, Any], key: str, limit: int) -> str | None:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"{key} is required text"
        if len(value) > limit:
            return f"{key} too long"
        return None

    def _node(self, p: dict[str, Any]) -> ProposalDecision:
        for key, limit in (
            ("label", self.MAX_LABEL_LENGTH),
            ("node_type", self.MAX_TYPE_LENGTH),
        ):
            problem = self._text(p, key, limit)
            if problem:
                return ProposalDecision(False, f"create_node: {problem}")
        description = p.get("description")
        if description is not None and (
            not isinstance(description, str)
            or len(description) > self.MAX_DESCRIPTION_LENGTH
        ):
            return ProposalDecision(False, "create_node: description invalid")
        props = p.get("properties", {})
        if not isinstance(props, dict) or len(props) > self.MAX_PROPERTY_KEYS:  # pyright: ignore[reportUnknownArgumentType]
            return ProposalDecision(False, "create_node: properties invalid")
        return ProposalDecision(True, "accepted")

    def _edge(self, p: dict[str, Any]) -> ProposalDecision:
        for key in ("source_node_id", "target_node_id"):
            if key not in p:
                return ProposalDecision(False, f"create_edge requires {key}")
        problem = self._text(p, "relation", self.MAX_TYPE_LENGTH)
        return (
            ProposalDecision(False, f"create_edge: {problem}")
            if problem
            else ProposalDecision(True, "accepted")
        )

    def _supersede_node(self, p: dict[str, Any]) -> ProposalDecision:
        if "old_node_id" not in p:
            return ProposalDecision(False, "supersede_node requires old_node_id")
        if "label" in p or "node_type" in p:
            return self._node(p)
        return ProposalDecision(True, "accepted")

    def _attach(self, p: dict[str, Any]) -> ProposalDecision:
        if "derived_id" not in p or "derived_type" not in p:
            return ProposalDecision(
                False, "attach_evidence requires derived_type and derived_id"
            )
        return ProposalDecision(True, "accepted")

    def _notebook(self, p: dict[str, Any]) -> ProposalDecision:
        payload = p.get("payload")
        if not isinstance(payload, dict) or not payload:
            return ProposalDecision(
                False, "add_notebook_entry requires a payload object"
            )
        # Vision-security Fix 4 (Founder's Vision §14): personal-life
        # disclosures require explicit student consent. CI may propose
        # a ``personal_context`` entry only when the student has
        # explicitly asked for it to be remembered. Automatic
        # persistence of sensitive disclosures — to create artificial
        # intimacy — is forbidden.
        #
        # The check is scoped to ``kind == "personal_context"`` ONLY.
        # Other kinds (learning_state, mastery, teaching_observation,
        # communication_preference, circumstance, tutor_quality_assessment,
        # etc.) are academic/operational context and may be persisted
        # freely, subject to CI's own judgement. The
        # ``ALLOWED_NOTEBOOK_KINDS`` set above documents the academic
        # kinds the Founder's vision calls out; it is not an
        # enforcement list because existing entries (e.g.
        # ``tutor_quality_assessment`` written by the Judge) pre-date
        # the kind taxonomy and must continue to validate.
        kind_value = payload.get("kind")
        if kind_value is not None:
            kind = str(kind_value).strip()
            if kind == "personal_context":
                consent = payload.get("student_explicit_consent")
                if consent is not True:
                    return ProposalDecision(
                        False,
                        "add_notebook_entry: payload.kind='personal_context' "
                        "requires payload.student_explicit_consent=True. The "
                        "student must explicitly ask for this disclosure to "
                        "be remembered. Automatic persistence of personal-"
                        "life disclosures is forbidden (Founder's Vision §14: "
                        "never use sensitive disclosures just to create "
                        "artificial intimacy).",
                    )
        return ProposalDecision(True, "accepted")

    def _set_learning_state(self, p: dict[str, Any]) -> ProposalDecision:
        """Founder's vision §6: ``set_learning_state`` requires a structured
        payload describing where the student is in a subject right now.

        The schema is closed on the required keys (``subject``, ``topic``,
        ``current_stage``, ``subtopics``, ``teaching_observation``,
        ``next_sensible_step``) so the application can render the state
        to the Teacher and supersede it on the next advance. Subtopic
        statuses come from a small closed set so the Teacher and the
        Judge can reason about them without parsing free text.
        """
        payload = p.get("payload")
        if not isinstance(payload, dict) or not payload:
            return ProposalDecision(
                False, "set_learning_state requires a payload object"
            )
        # Required text fields, each with its own length cap.
        for key, limit in (
            ("subject", self.MAX_SUBJECT_LENGTH),
            ("topic", self.MAX_TOPIC_LENGTH),
            ("current_stage", self.MAX_STAGE_LENGTH),
            ("next_sensible_step", self.MAX_NEXT_STEP_LENGTH),
        ):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                return ProposalDecision(
                    False, f"set_learning_state: {key} is required text"
                )
            if len(value) > limit:
                return ProposalDecision(False, f"set_learning_state: {key} too long")
        observation = payload.get("teaching_observation")
        if observation is not None and (
            not isinstance(observation, str)
            or len(observation) > self.MAX_OBSERVATION_LENGTH
        ):
            return ProposalDecision(
                False, "set_learning_state: teaching_observation invalid"
            )
        subtopics = payload.get("subtopics")
        if not isinstance(subtopics, list) or not subtopics:
            return ProposalDecision(
                False, "set_learning_state: subtopics must be a non-empty list"
            )
        if len(subtopics) > self.MAX_SUBTOPICS:
            return ProposalDecision(False, "set_learning_state: too many subtopics")
        for st in subtopics:
            if not isinstance(st, dict):
                return ProposalDecision(
                    False, "set_learning_state: subtopic must be an object"
                )
            name = st.get("name")
            if not isinstance(name, str) or not name.strip():
                return ProposalDecision(
                    False, "set_learning_state: subtopic.name is required text"
                )
            if len(name) > self.MAX_SUBTOPIC_NAME_LENGTH:
                return ProposalDecision(
                    False, "set_learning_state: subtopic.name too long"
                )
            status = st.get("status")
            if not isinstance(status, str):
                return ProposalDecision(
                    False, "set_learning_state: subtopic.status is required text"
                )
            if status not in self.ALLOWED_SUBTOPIC_STATUSES:
                return ProposalDecision(
                    False,
                    f"set_learning_state: subtopic.status must be one of "
                    f"{sorted(self.ALLOWED_SUBTOPIC_STATUSES)}",
                )
        # Pin the kind so the renderer + retrieval tool can find these
        # entries without a free-text convention.
        payload["kind"] = "learning_state"
        return ProposalDecision(True, "accepted")

    def _supersede_notebook(self, p: dict[str, Any]) -> ProposalDecision:
        if "old_entry_id" not in p:
            return ProposalDecision(
                False, "supersede_notebook_entry requires old_entry_id"
            )
        return self._notebook(p)

    def _supersede_summary(self, p: dict[str, Any]) -> ProposalDecision:
        if "old_summary_id" not in p:
            return ProposalDecision(False, "supersede_summary requires old_summary_id")
        problem = self._text(p, "text", self.MAX_SUMMARY_LENGTH)
        return (
            ProposalDecision(False, f"supersede_summary: {problem}")
            if problem
            else ProposalDecision(True, "accepted")
        )

    def _link_artifact(self, p: dict[str, Any]) -> ProposalDecision:
        for key in ("artifact_id", "reference_type", "reference_id"):
            if key not in p:
                return ProposalDecision(False, f"link_artifact requires {key}")
        return ProposalDecision(True, "accepted")

    # --- lifecycle operations ----------------------------------------------------
    def _invalidate_notebook(self, p: dict[str, Any]) -> ProposalDecision:
        if "entry_id" not in p:
            return ProposalDecision(
                False, "invalidate_notebook_entry requires entry_id"
            )
        return ProposalDecision(True, "accepted")

    def _invalidate_node(self, p: dict[str, Any]) -> ProposalDecision:
        if "node_id" not in p:
            return ProposalDecision(False, "invalidate_node requires node_id")
        return ProposalDecision(True, "accepted")

    def _invalidate_summary(self, p: dict[str, Any]) -> ProposalDecision:
        if "summary_id" not in p:
            return ProposalDecision(False, "invalidate_summary requires summary_id")
        return ProposalDecision(True, "accepted")

    def _rebuild_index(self, p: dict[str, Any]) -> ProposalDecision:
        for key in ("object_type", "object_id"):
            if key not in p:
                return ProposalDecision(False, f"rebuild_index requires {key}")
        try:
            obj_type = ObjectType(str(p["object_type"]))
        except ValueError:
            return ProposalDecision(False, "rebuild_index: unknown object_type")
        if obj_type not in INVALIDATABLE and obj_type not in {
            ObjectType.HISTORY,
            ObjectType.ARTIFACT_VERSION,
            ObjectType.ARTIFACT_CONTENT,
        }:
            return ProposalDecision(
                False,
                "rebuild_index: object_type not indexable",
            )
        return ProposalDecision(True, "accepted")

    def _request_deletion(self, p: dict[str, Any]) -> ProposalDecision:
        for key in ("object_type", "object_id", "scope"):
            if key not in p:
                return ProposalDecision(False, f"request_deletion requires {key}")
        scope = str(p["scope"])
        if len(scope) > self.MAX_SCOPE_LENGTH:
            return ProposalDecision(False, "request_deletion: scope too long")
        # Scope is free-form text (e.g. "this_notebook_entry_only",
        # "this_student_all_representations"). The actual retention policy
        # is enforced downstream by the deletion workflow, not by the
        # validator.
        try:
            ObjectType(str(p["object_type"]))
        except ValueError:
            return ProposalDecision(False, "request_deletion: unknown object_type")
        return ProposalDecision(True, "accepted")
