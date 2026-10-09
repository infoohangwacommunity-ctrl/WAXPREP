"""Tool schemas exposed to the Context Intelligence model."""

from __future__ import annotations

from typing import Any


def context_tool_definitions() -> list[dict[str, Any]]:
    """Capabilities for CI — not database implementation details."""
    return [
        {
            "name": "search_conversation",
            "description": (
                "Search the student's past conversation evidence for "
                "something relevant to the current question."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "conversation_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_notebook",
            "description": (
                "Search the student's open-ended notebook for useful information."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_workspace",
            "description": (
                "Search workspace/artifact metadata relevant to the question."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
            },
        },
        {
            "name": "inspect_evidence",
            "description": "Inspect a specific evidence item more deeply.",
            "input_schema": {
                "type": "object",
                "properties": {"evidence_id": {"type": "string"}},
                "required": ["evidence_id"],
            },
        },
        {
            "name": "follow_relationships",
            "description": (
                "Follow relationships from a knowledge node to connected items."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["outgoing", "incoming", "both"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["node_id"],
            },
        },
        {
            "name": "search_semantically",
            "description": (
                "Search by meaning when wording may differ from the original."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
            },
        },
        {
            "name": "check_history",
            "description": (
                "Check whether a fact changed, was superseded, or remains current."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
    ]
