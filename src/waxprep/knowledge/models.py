"""Open-world knowledge graph primitives.

No fixed student-profile schema (school/class/strengths/etc.).
Nodes and edges are generic; meaning is decided by Context Intelligence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId


class KnowledgeNodeStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DORMANT = "dormant"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class KnowledgeNode:
    id: UUID
    wax_id: WaxId
    node_type: str
    label: str
    description: str | None
    properties: dict[str, Any]
    status: KnowledgeNodeStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeEdge:
    id: UUID
    wax_id: WaxId
    source_node_id: UUID
    target_node_id: UUID
    relation: str
    properties: dict[str, Any]
    valid_from: datetime | None
    valid_until: datetime | None
    created_at: datetime


__all__ = [
    "KnowledgeEdge",
    "KnowledgeNode",
    "KnowledgeNodeStatus",
]
