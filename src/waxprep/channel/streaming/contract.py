"""Shared streaming contract — provider-neutral, channel-aware.

Blueprint §4: one common contract, independent implementations per platform.

The contract separates five concepts (blueprint §3.1):
- Generation (model provider)
- Accumulation (AnswerAccumulator)
- Presentation preparation (StreamSafeFormatter)
- Progressive delivery (ChannelAdapter)
- Finalization (turn orchestration + outbox)

The Teacher decides what to say. The accumulator preserves what it said.
The formatter makes it safe to display. The channel adapter uses the
real capabilities of the destination. The outbox preserves the final
answer and its delivery state.

No hardcoded educational rules. The pacing policy balances
responsiveness, readability, network traffic and platform limits —
it never decides what the student needs to learn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AnswerStreamEventType(StrEnum):
    """Closed vocabulary of answer-stream events.

    Only ``ANSWER_DELTA`` and ``ANSWER_COMPLETED`` carry student-facing
    answer text. All other events are internal and must never be rendered
    as answer text (blueprint §4.1, §7.7).
    """

    RESPONSE_STARTED = "response_started"
    ANSWER_DELTA = "answer_delta"
    ANSWER_SNAPSHOT = "answer_snapshot"
    ANSWER_COMPLETED = "answer_completed"
    RESPONSE_FAILED = "response_failed"
    RESPONSE_CANCELLED = "response_cancelled"
    STREAM_FALLBACK_STARTED = "stream_fallback_started"
    USAGE_REPORTED = "usage_reported"


class ResponseLifecycle(StrEnum):
    """The lifecycle states of one streaming response.

    A response moves through these states in order. Cancellation can
    occur from any active state. The coordinator must not accept deltas
    for a response that is not ACTIVE (blueprint §9.4).
    """

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChannelCapability(StrEnum):
    """Capabilities a channel adapter may report.

    The adapter reports only capabilities that have been verified for
    the particular API integration. A platform name alone must not
    imply that every API method is available (blueprint §4.2).
    """

    NATIVE_TEXT_DRAFT_UPDATES = "native_text_draft_updates"
    NATIVE_RICH_DRAFT_UPDATES = "native_rich_draft_updates"
    COMPLETE_MESSAGE_SEND = "complete_message_send"
    TYPING_INDICATOR = "typing_indicator"
    MESSAGE_EDIT = "message_edit"
    MESSAGE_RECEIPTS = "message_receipts"
    SUPPORTS_FINAL_PERSISTENT_MESSAGE = "supports_final_persistent_message"
    SUPPORTS_STOP_GENERATION = "supports_stop_generation"
    SUPPORTS_MESSAGE_THREAD = "supports_message_thread"


class DeliveryStrategy(StrEnum):
    """The delivery strategies the coordinator may select.

    These are delivery strategies, not educational response modes. The
    Teacher is never instructed to write a different answer because
    the channel has a different API (blueprint §4.3).
    """

    NATIVE_DRAFT = "native_draft"
    PROGRESSIVE_COMPLETE_MESSAGES = "progressive_complete_messages"
    FINAL_ONLY = "final_only"
    TYPING_PLUS_FINAL = "typing_plus_final"


@dataclass(frozen=True, slots=True)
class AnswerStreamEvent:
    """One event in the answer stream.

    Only ``text`` on ``ANSWER_DELTA`` / ``ANSWER_SNAPSHOT`` /
    ``ANSWER_COMPLETED`` is student-facing answer content. All other
    fields are internal metadata for the coordinator and observability.
    """

    type: AnswerStreamEventType
    text: str | None = None
    response_id: UUID | None = None
    sequence: int = 0
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """The capabilities a channel adapter has verified.

    The adapter reports only what it has actually tested. An unverified
    capability must not be reported (blueprint §4.2).
    """

    channel: str
    capabilities: frozenset[ChannelCapability] = frozenset()
    max_text_size: int | None = None
    supported_formatting: tuple[str, ...] = ()

    def has(self, cap: ChannelCapability) -> bool:
        return cap in self.capabilities


__all__ = [
    "AnswerStreamEvent",
    "AnswerStreamEventType",
    "ChannelCapabilities",
    "ChannelCapability",
    "DeliveryStrategy",
    "ResponseLifecycle",
]
