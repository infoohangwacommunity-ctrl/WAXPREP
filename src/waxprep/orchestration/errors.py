"""Typed orchestration failures.

The turn orchestrator and the model-backed Teacher raise these errors
instead of returning invalid data. A typed failure keeps the already-saved
student message intact and lets the caller (or channel retry) resume the
turn safely. Error messages never include provider payloads, secrets, or
student message content.
"""

from __future__ import annotations


class OrchestrationError(Exception):
    """Base class for orchestration failures."""


class TeacherModelError(OrchestrationError):
    """The model-backed Teacher received unusable output from the model.

    Raised for malformed structured decisions, unknown action values,
    missing required fields, blank final text, or a decision that violates
    the consultation bounds. The turn fails in a controlled way: the saved
    student message is preserved and no tutor reply is persisted.
    """


class InvalidTeacherOutcomeError(OrchestrationError):
    """A Teacher returned an outcome that cannot be persisted as a reply.

    Raised by the turn orchestrator when a final outcome has no usable
    text, or when the forced final round ends unfinished. An empty or
    whitespace-only reply is never persisted as a tutor entry.
    """


__all__ = [
    "InvalidTeacherOutcomeError",
    "OrchestrationError",
    "TeacherModelError",
]
