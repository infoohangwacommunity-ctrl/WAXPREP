"""Open-world knowledge has no fixed student-profile fields."""

from datetime import UTC, datetime
from uuid import uuid4

from waxprep.domain.identifiers import new_wax_id
from waxprep.knowledge.models import KnowledgeNode, KnowledgeNodeStatus


def test_knowledge_node_has_no_fixed_student_profile_fields() -> None:
    now = datetime.now(UTC)
    node = KnowledgeNode(
        id=uuid4(),
        wax_id=new_wax_id(),
        node_type="whatever_the_ai_discovered",
        label="Something the student mentioned",
        description="An open-ended concept.",
        properties={
            "anything": "the AI found useful",
            "unexpected_relationship": {"reason": "example"},
        },
        status=KnowledgeNodeStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    assert node.node_type == "whatever_the_ai_discovered"
    assert "anything" in node.properties


def test_open_world_does_not_require_student_profile_fields() -> None:
    now = datetime.now(UTC)
    node = KnowledgeNode(
        id=uuid4(),
        wax_id=new_wax_id(),
        node_type="new_concept_not_predicted_by_product",
        label="A completely new concept",
        description="Something discovered naturally.",
        properties={"student_language": "whatever the model found useful"},
        status=KnowledgeNodeStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    assert node.properties["student_language"] == "whatever the model found useful"
