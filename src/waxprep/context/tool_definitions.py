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

# Lifecycle operations the model may propose via the
# ``propose_lifecycle_change`` tool. The application validates the
# arguments and authorises the change through EvolutionService — the
# model never writes to storage directly.
_LIFECYCLE_OPERATIONS: list[str] = [
    "add_notebook_entry",
    "supersede_notebook_entry",
    "supersede_summary",
    "supersede_node",
    "create_node",
    "create_edge",
    "attach_evidence",
    "link_artifact",
    "invalidate_notebook_entry",
    "invalidate_node",
    "invalidate_summary",
    "rebuild_index",
    "request_deletion",
]


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
        # --- write-side proposal tool (Blueprint §28-29) ---------------------
        # The model proposes a lifecycle change; the application validates
        # the proposal, checks that the cited evidence exists and belongs
        # to the same student, and applies it through EvolutionService.
        # The model never writes to storage directly. Tool-argument
        # validation, ownership checks, and student-scoped reads happen
        # on the application side — never trust the model's ids.
        _tool(
            "propose_lifecycle_change",
            "Propose a lifecycle change to a derived object. The application "
            "validates the proposal and applies it; you never write to "
            "storage directly. Only cite evidence you have actually gathered "
            "this turn (use find_exact / find_by_meaning / browse_time_window "
            "/ read_around / trace_sources first). Every proposal must cite "
            "at least one piece of evidence. Invalid or unauthorised proposals "
            "are rejected and the rejection is recorded in the audit trail.",
            {
                "operation": {
                    "type": "string",
                    "enum": _LIFECYCLE_OPERATIONS,
                    "description": "The lifecycle operation to propose.",
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "Operation-specific arguments. For add_notebook_entry: "
                        "{'payload': {<open-ended note>}}. For supersede_*: "
                        "include 'old_*_id' plus the new fields. For "
                        "invalidate_*: {'entry_id' | 'node_id' | 'summary_id'}. "
                        "For rebuild_index: {'object_type', 'object_id'}. "
                        "For request_deletion: {'object_type', 'object_id', "
                        "'scope'}."
                    ),
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "object_type": {"type": "string"},
                            "object_id": {"type": "string"},
                            "quote": {
                                "type": "string",
                                "description": (
                                    "Optional short quote from the evidence. "
                                    "Long quotes are truncated."
                                ),
                            },
                        },
                        "required": ["object_type", "object_id"],
                    },
                    "description": (
                        "Objects that support the proposal. Every proposal "
                        "must cite at least one piece of evidence gathered "
                        "this turn (or earlier and still valid)."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short, opaque reason for the proposal. The "
                        "application may redact or truncate it. Avoid "
                        "quoting student content here — use evidence_refs "
                        "for that."
                    ),
                },
            },
            ["operation", "payload", "evidence_refs"],
        ),
    ]
