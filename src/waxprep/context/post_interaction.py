"""Write-side proposal boundary for knowledge updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId


@dataclass(frozen=True, slots=True)
class KnowledgeProposal:
    wax_id: WaxId
    source_conversation_id: UUID
    operation: str
    payload: dict[str, Any]
    evidence_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    accepted: bool
    reason: str
    proposal: KnowledgeProposal


class KnowledgeProposalValidator:
    MAX_LABEL_LENGTH = 500
    MAX_PROPERTY_KEYS = 32

    def validate(self, proposal: KnowledgeProposal) -> ProposalDecision:
        payload = proposal.payload
        label = payload.get("label")
        if label is not None:
            if not isinstance(label, str):
                return ProposalDecision(
                    accepted=False, reason="label must be text", proposal=proposal
                )
            if len(label) > self.MAX_LABEL_LENGTH:
                return ProposalDecision(
                    accepted=False, reason="label too long", proposal=proposal
                )
        if len(payload) > self.MAX_PROPERTY_KEYS:
            return ProposalDecision(
                accepted=False,
                reason="too many arbitrary properties",
                proposal=proposal,
            )
        return ProposalDecision(
            accepted=True,
            reason="proposal passed structural validation",
            proposal=proposal,
        )
