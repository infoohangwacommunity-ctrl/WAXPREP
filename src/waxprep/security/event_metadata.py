"""Event metadata contract (spec section 16 / finding 6).

Defines the boundary between safe operational metadata and sensitive
student-derived content. The ``events.metadata`` JSONB column must
only contain safe operational keys — never student text, quotes, or
other sensitive content.

Sensitive derived information is stored encrypted in the appropriate
student-scoped table (history_entries, notebook_entries, summaries,
etc.), never in event metadata.
"""

from __future__ import annotations

from typing import Any, cast

# Allowed operational metadata keys. These are non-sensitive values
# useful for debugging and operational monitoring. The list is
# exhaustive — any key not in this set is rejected.
ALLOWED_EVENT_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "error_code",  # opaque operational error code
        "error_class",  # exception class name (not the message)
        "source",  # which component emitted the event
        "duration_ms",  # timing
        "attempt",  # retry attempt number
        "model",  # model name (not student content)
        "kind",  # sub-kind of the event
        "status",  # operational status
        "wax_id",  # student identifier (not student content)
        "object_type",  # structural type
        "object_id",  # structural id
        "request_id",  # UUID of a deletion/lifecycle request (not student content)
        "investigation_status",  # closed-vocabulary status of one CI investigation
        "trigger_entry_id",  # UUID of the student entry that triggered an investigation
        # Operational quality signals (no student plaintext):
        "evidence_count",  # integer count of CI evidence items
        "invalid_selection",  # bool — CI selection contract failure
        "notebook_proposal",  # "accepted" | "none" | "rejected"
        "reply_chars",  # length of tutor reply (not the text)
        "exchange_included",  # bool — the Teacher received the live exchange
        "exchange_turns",  # integer count of exchange turns carried
        # Quality Judge signals (operational, no student plaintext):
        "rating",  # integer 1-5 overall tutor-quality rating
        "confidence",  # closed vocabulary: low | medium | high
        "window_turns",  # integer count of student-tutor pairs evaluated
    }
)

# Maximum metadata size (bytes after JSON serialization) to prevent
# accidental large-content leaks.
MAX_EVENT_METADATA_JSON_BYTES = 4096


class EventMetadataContractError(ValueError):
    """Raised when event metadata contains a disallowed key or content."""


def validate_event_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Validate event metadata against the operational-only contract.

    Returns the validated metadata. Raises ``EventMetadataContractError``
    if the metadata contains:
    - a key not in ``ALLOWED_EVENT_METADATA_KEYS``
    - a value longer than 200 characters (to prevent student text leaks)
    - a total JSON-serialized size exceeding 4 KB
    """
    if metadata is None:
        return cast(dict[str, Any], {})
    if not isinstance(metadata, dict):
        raise EventMetadataContractError(
            f"event metadata must be a dict, not {type(metadata).__name__}"
        )
    for key, value in metadata.items():
        if key not in ALLOWED_EVENT_METADATA_KEYS:
            raise EventMetadataContractError(
                f"event metadata key {key!r} is not in the allowed "
                f"operational set; sensitive content must go in the "
                f"appropriate encrypted student-scoped table, not event "
                f"metadata"
            )
        # Check value length — long values may contain student text.
        if isinstance(value, str) and len(value) > 200:
            raise EventMetadataContractError(
                f"event metadata value for {key!r} exceeds 200 "
                f"characters; student-derived content must not be stored "
                f"in event metadata"
            )
    # Check total JSON size.
    import json

    raw = json.dumps(metadata)
    if len(raw.encode("utf-8")) > MAX_EVENT_METADATA_JSON_BYTES:
        raise EventMetadataContractError(
            f"event metadata JSON exceeds {MAX_EVENT_METADATA_JSON_BYTES} bytes; "
            f"sensitive content must not be stored in event metadata"
        )
    return metadata


__all__ = [
    "ALLOWED_EVENT_METADATA_KEYS",
    "EventMetadataContractError",
    "MAX_EVENT_METADATA_JSON_BYTES",
    "validate_event_metadata",
]

