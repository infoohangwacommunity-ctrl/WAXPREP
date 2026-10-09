"""Relationships require explicit proposals with evidence."""

from datetime import UTC, datetime
from uuid import uuid4

from waxprep.context.graph_builder import RelationshipProposal, proposal_to_edge
from waxprep.domain.identifiers import new_wax_id


def test_relationship_requires_proposal() -> None:
    source = uuid4()
    target = uuid4()
    proposal = RelationshipProposal(
        source_node_id=source,
        target_node_id=target,
        relation="appears_related_to",
        properties={},
        reason="Evidence suggests a relationship.",
        evidence_ids=(uuid4(),),
    )
    edge = proposal_to_edge(proposal, wax_id=new_wax_id(), now=datetime.now(UTC))
    assert edge.source_node_id == source
    assert edge.target_node_id == target
    assert edge.properties["evidence_ids"]
