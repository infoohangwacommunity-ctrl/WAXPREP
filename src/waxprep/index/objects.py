"""Reading the searchable text of any stored object by type and id."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import ObjectType
from waxprep.storage.protocol import Storage


def _join(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def describe_object(
    storage: Storage, wax_id: WaxId, object_type: ObjectType, object_id: UUID
) -> tuple[str, datetime] | None:
    """Return (searchable text, object time) or None when it does not exist."""
    if object_type is ObjectType.HISTORY:
        entry = storage.get_history_entry(wax_id, object_id)
        if entry is None or not entry.text:
            return None
        return entry.text, entry.occurred_at
    if object_type is ObjectType.SUMMARY:
        summary = storage.get_summary(wax_id, object_id)
        if summary is None:
            return None
        return summary.text, summary.covers_until
    if object_type is ObjectType.NOTEBOOK:
        note = storage.get_notebook_entry(wax_id, object_id)
        if note is None:
            return None
        return flatten_payload(note.payload), note.created_at
    if object_type is ObjectType.KNOWLEDGE_NODE:
        node = storage.get_node(wax_id, object_id)
        if node is None:
            return None
        return _join(node.node_type, node.label, node.description), node.created_at
    if object_type is ObjectType.ARTIFACT_VERSION:
        version = storage.get_artifact_version_by_id(wax_id, object_id)
        if version is None:
            return None
        text = _join(version.original_filename, version.mime_type)
        return (text, version.created_at) if text else None
    if object_type is ObjectType.ARTIFACT_CONTENT:
        content = storage.get_artifact_content(wax_id, object_id)
        if content is None:
            return None
        return content.text, content.created_at
    return None


def text_for_object(
    storage: Storage, wax_id: WaxId, object_type: ObjectType, object_id: UUID
) -> str | None:
    found = describe_object(storage, wax_id, object_type, object_id)
    return found[0] if found else None


def flatten_payload(value: object) -> str:
    """Flatten any open-ended JSON-like payload into searchable text."""
    if isinstance(value, dict):
        return " ".join(
            f"{key} {flatten_payload(inner)}"
            for key, inner in value.items()  # pyright: ignore[reportUnknownVariableType]
        )
    if isinstance(value, (list, tuple)):
        return " ".join(flatten_payload(inner) for inner in value)  # pyright: ignore[reportUnknownVariableType]
    return str(value)
