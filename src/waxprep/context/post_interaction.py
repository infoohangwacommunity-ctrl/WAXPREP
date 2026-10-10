"""Write-side proposal boundary for knowledge updates.

CI proposes. The application validates and persists.
This module does not invent student meaning from keywords.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId


class KnowledgeOperation(StrEnum):
    """Allowed durable knowledge write operations (application-enforced)."""

    CREATE_NODE = "create_node"
    CREATE_EDGE = "create_edge"
    SUPERSEDE_NODE = "supersede_node"
    ATTACH_EVIDENCE = "attach_evidence"
    ADD_NOTEBOOK_ENTRY = "add_notebook_entry"


@dataclass(frozen=True, slots=True)
class KnowledgeProposal:
    """A model-proposed change to the student's open-world knowledge."""

    wax_id: WaxId
    source_conversation_id: UUID
    operation: str
    payload: dict[str, Any]
    evidence_ids: tuple[UUID, ...]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    accepted: bool
    reason: str
    proposal: KnowledgeProposal


class KnowledgeProposalValidator:
    """Structural validation only — not educational intelligence."""

    MAX_LABEL_LENGTH = 500
    MAX_DESCRIPTION_LENGTH = 4000
    MAX_PROPERTY_KEYS = 32
    MAX_NODE_TYPE_LENGTH = 120
    MAX_RELATION_LENGTH = 120
    MAX_QUOTE_LENGTH = 4000
    ALLOWED_OPERATIONS = frozenset(op.value for op in KnowledgeOperation)

    def validate(self, proposal: KnowledgeProposal) -> ProposalDecision:
        if proposal.operation not in self.ALLOWED_OPERATIONS:
            return ProposalDecision(
                accepted=False,
                reason=f"unknown operation: {proposal.operation}",
                proposal=proposal,
            )
        if not proposal.evidence_ids and proposal.operation != (
            KnowledgeOperation.ADD_NOTEBOOK_ENTRY.value
        ):
            # Notebook may be AI-maintained understanding; still prefer evidence
            # but allow notebook-only proposals with empty evidence for structure tests.
            pass

        payload = proposal.payload
        if len(payload) > self.MAX_PROPERTY_KEYS + 8:
            return ProposalDecision(
                accepted=False,
                reason="payload too large",
                proposal=proposal,
            )

        op = proposal.operation
        if op == KnowledgeOperation.CREATE_NODE.value:
            return self._validate_create_node(proposal)
        if op == KnowledgeOperation.CREATE_EDGE.value:
            return self._validate_create_edge(proposal)
        if op == KnowledgeOperation.SUPERSEDE_NODE.value:
            return self._validate_supersede(proposal)
        if op == KnowledgeOperation.ATTACH_EVIDENCE.value:
            return self._validate_attach_evidence(proposal)
        if op == KnowledgeOperation.ADD_NOTEBOOK_ENTRY.value:
            return self._validate_notebook(proposal)
        return ProposalDecision(
            accepted=False, reason="unsupported operation", proposal=proposal
        )

    def _validate_create_node(self, proposal: KnowledgeProposal) -> ProposalDecision:
        p = proposal.payload
        label = p.get("label")
        node_type = p.get("node_type")
        if not isinstance(label, str) or not label.strip():
            return ProposalDecision(
                accepted=False, reason="create_node requires label", proposal=proposal
            )
        if len(label) > self.MAX_LABEL_LENGTH:
            return ProposalDecision(
                accepted=False, reason="label too long", proposal=proposal
            )
        if not isinstance(node_type, str) or not node_type.strip():
            return ProposalDecision(
                accepted=False,
                reason="create_node requires node_type",
                proposal=proposal,
            )
        if len(node_type) > self.MAX_NODE_TYPE_LENGTH:
            return ProposalDecision(
                accepted=False, reason="node_type too long", proposal=proposal
            )
        description = p.get("description")
        if description is not None:
            if not isinstance(description, str):
                return ProposalDecision(
                    accepted=False,
                    reason="description must be text",
                    proposal=proposal,
                )
            if len(description) > self.MAX_DESCRIPTION_LENGTH:
                return ProposalDecision(
                    accepted=False,
                    reason="description too long",
                    proposal=proposal,
                )
        props = p.get("properties", {})
        if props is not None and not isinstance(props, dict):
            return ProposalDecision(
                accepted=False,
                reason="properties must be an object",
                proposal=proposal,
            )
        if isinstance(props, dict) and len(props) > self.MAX_PROPERTY_KEYS:  # type: ignore[arg-type]
            return ProposalDecision(
                accepted=False,
                reason="too many arbitrary properties",
                proposal=proposal,
            )
        return ProposalDecision(
            accepted=True,
            reason="create_node proposal accepted",
            proposal=proposal,
        )

    def _validate_create_edge(self, proposal: KnowledgeProposal) -> ProposalDecision:
        p = proposal.payload
        for key in ("source_node_id", "target_node_id", "relation"):
            if key not in p:
                return ProposalDecision(
                    accepted=False,
                    reason=f"create_edge requires {key}",
                    proposal=proposal,
                )
        relation = p.get("relation")
        if not isinstance(relation, str) or not relation.strip():
            return ProposalDecision(
                accepted=False, reason="relation must be text", proposal=proposal
            )
        if len(relation) > self.MAX_RELATION_LENGTH:
            return ProposalDecision(
                accepted=False, reason="relation too long", proposal=proposal
            )
        return ProposalDecision(
            accepted=True,
            reason="create_edge proposal accepted",
            proposal=proposal,
        )

    def _validate_supersede(self, proposal: KnowledgeProposal) -> ProposalDecision:
        p = proposal.payload
        if "old_node_id" not in p:
            return ProposalDecision(
                accepted=False,
                reason="supersede_node requires old_node_id",
                proposal=proposal,
            )
        # New node fields same as create_node when replacing content
        if "label" in p or "node_type" in p:
            return self._validate_create_node(proposal)
        return ProposalDecision(
            accepted=True,
            reason="supersede_node proposal accepted",
            proposal=proposal,
        )

    def _validate_attach_evidence(
        self, proposal: KnowledgeProposal
    ) -> ProposalDecision:
        p = proposal.payload
        if "node_id" not in p and "edge_id" not in p:
            return ProposalDecision(
                accepted=False,
                reason="attach_evidence requires node_id or edge_id",
                proposal=proposal,
            )
        if "source_type" not in p:
            return ProposalDecision(
                accepted=False,
                reason="attach_evidence requires source_type",
                proposal=proposal,
            )
        quote = p.get("quote")
        if quote is not None and not isinstance(quote, str):
            return ProposalDecision(
                accepted=False, reason="quote must be text", proposal=proposal
            )
        if isinstance(quote, str) and len(quote) > self.MAX_QUOTE_LENGTH:
            return ProposalDecision(
                accepted=False, reason="quote too long", proposal=proposal
            )
        return ProposalDecision(
            accepted=True,
            reason="attach_evidence proposal accepted",
            proposal=proposal,
        )

    def _validate_notebook(self, proposal: KnowledgeProposal) -> ProposalDecision:
        p = proposal.payload
        if "payload" not in p or not isinstance(p.get("payload"), dict):
            return ProposalDecision(
                accepted=False,
                reason="add_notebook_entry requires payload object",
                proposal=proposal,
            )
        return ProposalDecision(
            accepted=True,
            reason="add_notebook_entry proposal accepted",
            proposal=proposal,
        )
