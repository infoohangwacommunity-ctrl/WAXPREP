"""Streaming observability — measure the experience, not just code paths.

Blueprint §11: the system needs telemetry that proves whether streaming
actually improves the student's experience. Use monotonic time for
elapsed durations, durable timestamps for correlation. Avoid logging
full student answers by default.

This module records the timings and counters the blueprint requires
(§11.1, §11.2). The metrics are structured — not free-form log text —
so they can be aggregated and compared across runs.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class StreamingMetrics:
    """Per-response streaming metrics (blueprint §11.1).

    Every timing is a monotonic duration (seconds) or a durable
    timestamp for correlation. No student answer content is recorded.
    """

    response_id: UUID
    channel: str
    # Durations (seconds from the response start).
    time_to_first_provider_delta: float | None = None
    time_to_first_safe_output: float | None = None
    time_to_first_update_attempt: float | None = None
    time_to_first_acknowledged_update: float | None = None
    time_to_complete: float | None = None
    time_to_final_message: float | None = None
    # Counts.
    update_count: int = 0
    outbound_message_count: int = 0
    retry_count: int = 0
    uncertain_delivery_count: int = 0
    # Outcome.
    streamed_successfully: bool = False
    fell_back_to_non_streaming: bool = False
    produced_no_answer_delta: bool = False
    draft_update_failures: int = 0
    final_message_failures: int = 0
    formatter_fallbacks: int = 0
    rate_limit_events: int = 0
    duplicate_delivery_detections: int = 0
    cancelled: bool = False
    final_text_matched_source: bool = True
    formatting_validation_failures: int = 0

    def log_summary(self) -> None:
        """Log a non-sensitive summary of this response's metrics."""
        logger.info(
            "streaming_metrics",
            extra={
                "response_id": str(self.response_id),
                "channel": self.channel,
                "streamed_successfully": self.streamed_successfully,
                "fell_back": self.fell_back_to_non_streaming,
                "time_to_first_delta": self.time_to_first_provider_delta,
                "time_to_first_update": self.time_to_first_update_attempt,
                "time_to_complete": self.time_to_complete,
                "time_to_final": self.time_to_final_message,
                "update_count": self.update_count,
                "outbound_count": self.outbound_message_count,
                "cancelled": self.cancelled,
                "draft_failures": self.draft_update_failures,
                "final_failures": self.final_message_failures,
                "formatter_fallbacks": self.formatter_fallbacks,
                "rate_limits": self.rate_limit_events,
                "duplicates_detected": self.duplicate_delivery_detections,
                "final_text_matched": self.final_text_matched_source,
            },
        )


@dataclass
class StreamingCounters:
    """Aggregate counters across all responses (blueprint §11.2).

    Use these to report streaming success rate, fallback rate, and
    failure patterns. Avoid recording unnecessary student content.
    """

    responses_streamed_successfully: int = 0
    responses_fell_back: int = 0
    responses_no_answer_delta: int = 0
    draft_update_failures: int = 0
    permanent_message_failures: int = 0
    formatter_fallbacks: int = 0
    rate_limit_events: int = 0
    duplicate_delivery_detections: int = 0
    cancelled_responses: int = 0
    final_text_mismatches: int = 0
    formatting_validation_failures: int = 0
    _failure_categories: Counter[str] = field(default_factory=Counter)

    def record(self, metrics: StreamingMetrics) -> None:
        """Record one response's metrics into the aggregate counters."""
        if metrics.streamed_successfully:
            self.responses_streamed_successfully += 1
        if metrics.fell_back_to_non_streaming:
            self.responses_fell_back += 1
        if metrics.produced_no_answer_delta:
            self.responses_no_answer_delta += 1
        self.draft_update_failures += metrics.draft_update_failures
        self.permanent_message_failures += metrics.final_message_failures
        self.formatter_fallbacks += metrics.formatter_fallbacks
        self.rate_limit_events += metrics.rate_limit_events
        self.duplicate_delivery_detections += metrics.duplicate_delivery_detections
        if metrics.cancelled:
            self.cancelled_responses += 1
        if not metrics.final_text_matched_source:
            self.final_text_mismatches += 1
        self.formatting_validation_failures += metrics.formatting_validation_failures

    def record_failure_category(self, category: str) -> None:
        """Record a failure by category (blueprint §11.2: 'Track failure
        categories separately from semantic quality failures')."""
        self._failure_categories[category] += 1

    def summary(self) -> dict[str, Any]:
        """Return a non-sensitive summary dict for logging."""
        return {
            "streamed_successfully": self.responses_streamed_successfully,
            "fell_back": self.responses_fell_back,
            "no_answer_delta": self.responses_no_answer_delta,
            "draft_failures": self.draft_update_failures,
            "final_failures": self.permanent_message_failures,
            "formatter_fallbacks": self.formatter_fallbacks,
            "rate_limits": self.rate_limit_events,
            "duplicates": self.duplicate_delivery_detections,
            "cancelled": self.cancelled_responses,
            "final_text_mismatches": self.final_text_mismatches,
            "formatting_failures": self.formatting_validation_failures,
            "failure_categories": dict(self._failure_categories),
        }


    def record_delta_success(self) -> None:
        """Record a successful delta delivery."""
        self.update_count += 1

    def record_delta_failure(self) -> None:
        """Record a failed delta delivery."""
        self.draft_update_failures += 1


__all__ = ["StreamingCounters", "StreamingMetrics"]
