"""Tool schemas exposed to the Context Intelligence model.

These describe capabilities, not database tables.
"""

from __future__ import annotations

from typing import Any

_LIMIT = {"type": "integer", "minimum": 1, "maximum": 8}
_TIME_FILTERS: dict[str, Any] = {
    "since": {"type": "string", "description": "ISO 8601 start (inclusive)"},
    "until": {"type": "string", "description": "ISO 8601 end (exclusive)"},
    "object_types": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional: limit to some kinds of stored items",
    },
    "ignore_recency": {
        "type": "boolean",
        "description": "True when old items matter as much as new ones",
    },
}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def context_tool_definitions() -> list[dict[str, Any]]:
    return [
        _tool(
            "find_exact",
            "Find stored items containing specific words, names, numbers or "
            "file names. Use when the exact wording matters.",
            {"query": {"type": "string"}, "limit": _LIMIT, **_TIME_FILTERS},
            ["query"],
        ),
        _tool(
            "find_by_meaning",
            "Find stored items with a similar meaning even if the wording differs.",
            {"query": {"type": "string"}, "limit": _LIMIT, **_TIME_FILTERS},
            ["query"],
        ),
        _tool(
            "browse_time_window",
            "List what happened in a period of the student's history.",
            {
                "since": _TIME_FILTERS["since"],
                "until": _TIME_FILTERS["until"],
                "limit": _LIMIT,
                "newest_first": {"type": "boolean"},
            },
            [],
        ),
        _tool(
            "read_around",
            "Read what happened just before and after one moment in history. "
            "Without an id it uses the student's newest message.",
            {
                "entry_id": {"type": "string"},
                "before": {"type": "integer", "minimum": 0, "maximum": 20},
                "after": {"type": "integer", "minimum": 0, "maximum": 20},
            },
            [],
        ),
        _tool(
            "inspect_item",
            "Look closely at one item you already found.",
            {"item_id": {"type": "string"}, "item_type": {"type": "string"}},
            ["item_id"],
        ),
        _tool(
            "trace_sources",
            "Show the original evidence behind a summary, notebook entry or "
            "relationship.",
            {"item_id": {"type": "string"}, "item_type": {"type": "string"}},
            ["item_id", "item_type"],
        ),
        _tool(
            "check_currency",
            "Check whether an item was replaced or may be out of date, and see "
            "what replaced it.",
            {"item_id": {"type": "string"}, "item_type": {"type": "string"}},
            ["item_id", "item_type"],
        ),
        _tool(
            "follow_relationships",
            "Follow relationships from a knowledge item to connected items.",
            {
                "node_id": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["outgoing", "incoming", "both"],
                },
                "limit": _LIMIT,
            },
            ["node_id"],
        ),
        _tool(
            "read_artifact",
            "Read the extracted contents of a stored artifact version "
            "(worksheet, document, image description, transcript, and so on).",
            {"version_id": {"type": "string"}},
            ["version_id"],
        ),
    ]
