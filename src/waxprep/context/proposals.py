"""Write-side proposals: Context Intelligence proposes, the application validates.

Every durable change must point at evidence. Nothing here interprets meaning.
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
    ALLOWED = frozenset(op.value for op in Operation)

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
        checks = {
            Operation.CREATE_NODE.value: self._node,
            Operation.CREATE_EDGE.value: self._edge,
            Operation.SUPERSEDE_NODE.value: self._supersede_node,
            Operation.ATTACH_EVIDENCE.value: self._attach,
            Operation.ADD_NOTEBOOK_ENTRY.value: self._notebook,
            Operation.SUPERSEDE_NOTEBOOK_ENTRY.value: self._supersede_notebook,
            Operation.SUPERSEDE_SUMMARY.value: self._supersede_summary,
            Operation.LINK_ARTIFACT.value: self._link_artifact,
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
        if not isinstance(p.get("payload"), dict) or not p["payload"]:
            return ProposalDecision(
                False, "add_notebook_entry requires a payload object"
            )
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
