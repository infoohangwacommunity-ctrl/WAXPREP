"""Streaming metrics and observability utilities.

This module provides metrics collection for streaming operations,
including counters, histograms, and success rate tracking.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class StreamingStats:
    """Aggregate streaming statistics."""

    total_attempts: int = 0
    successful_streams: int = 0
    fallback_to_complete: int = 0
    total_deltas: int = 0
    delta_failures: int = 0
    final_message_failures: int = 0

    # Timing (nanoseconds for precision)
    time_to_first_delta_ns: list[float] = field(default_factory=list)
    time_to_first_visible_ns: list[float] = field(default_factory=list)
    stream_duration_ns: list[float] = field(default_factory=list)

    # Per-model stats
    model_stats: dict[str, dict[str, Any]] = field(
        default_factory=lambda: defaultdict(dict)
    )

    def record_attempt(self, model: str | None = None) -> None:
        """Record a streaming attempt."""
        self.total_attempts += 1
        if model:
            self.model_stats[model]["attempts"] = self.model_stats[model].get(
                "attempts", 0
            ) + 1

    def record_success(self, model: str | None = None) -> None:
        """Record a successful streaming session."""
        self.successful_streams += 1
        if model:
            self.model_stats[model]["successes"] = self.model_stats[model].get(
                "successes", 0
            ) + 1

    def record_fallback(self, model: str | None = None) -> None:
        """Record a fallback to non-streaming."""
        self.fallback_to_complete += 1
        if model:
            self.model_stats[model]["fallbacks"] = self.model_stats[model].get(
                "fallbacks", 0
            ) + 1

    def record_delta(self) -> None:
        """Record a delta delivery."""
        self.total_deltas += 1

    def record_delta_failure(self) -> None:
        """Record a delta delivery failure."""
        self.delta_failures += 1

    def record_final_failure(self) -> None:
        """Record a final message delivery failure."""
        self.final_message_failures += 1

    def record_time_to_first_delta(
        self, duration_ns: float, model: str | None = None
    ) -> None:
        """Record time to first delta."""
        self.time_to_first_delta_ns.append(duration_ns)
        if model:
            self.model_stats[model]["time_to_first_delta_ns"] = (
                self.model_stats[model].get("time_to_first_delta_ns", [])
                + [duration_ns]
            )

    def record_time_to_first_visible(
        self, duration_ns: float, model: str | None = None
    ) -> None:
        """Record time to first visible output."""
        self.time_to_first_visible_ns.append(duration_ns)
        if model:
            self.model_stats[model]["time_to_first_visible_ns"] = (
                self.model_stats[model].get("time_to_first_visible_ns", [])
                + [duration_ns]
            )

    def record_stream_duration(
        self, duration_ns: float, model: str | None = None
    ) -> None:
        """Record stream duration."""
        self.stream_duration_ns.append(duration_ns)
        if model:
            self.model_stats[model]["stream_duration_ns"] = (
                self.model_stats[model].get("stream_duration_ns", [])
                + [duration_ns]
            )

    def get_success_rate(self) -> float:
        """Get the streaming success rate."""
        if self.total_attempts == 0:
            return 0.0
        return self.successful_streams / self.total_attempts

    def get_fallback_rate(self) -> float:
        """Get the fallback rate."""
        if self.total_attempts == 0:
            return 0.0
        return self.fallback_to_complete / self.total_attempts

    def get_avg_time_to_first_delta_ms(self) -> float:
        """Get average time to first delta in milliseconds."""
        if not self.time_to_first_delta_ns:
            return 0.0
        return (
            sum(self.time_to_first_delta_ns)
            / len(self.time_to_first_delta_ns)
            / 1_000_000
        )

    def get_avg_time_to_first_visible_ms(self) -> float:
        """Get average time to first visible output in milliseconds."""
        if not self.time_to_first_visible_ns:
            return 0.0
        return (
            sum(self.time_to_first_visible_ns)
            / len(self.time_to_first_visible_ns)
            / 1_000_000
        )

    def get_avg_stream_duration_ms(self) -> float:
        """Get average stream duration in milliseconds."""
        if not self.stream_duration_ns:
            return 0.0
        return sum(self.stream_duration_ns) / len(self.stream_duration_ns) / 1_000_000

    @staticmethod
    def _summarize_model(stats: dict[str, Any]) -> dict[str, Any]:
        """Summarize per-model streaming statistics for logging."""
        attempts = stats.get("attempts", 0)
        successes = stats.get("successes", 0)
        return {
            "attempts": attempts,
            "successes": successes,
            "fallbacks": stats.get("fallbacks", 0),
            "success_rate": f"{(successes / max(attempts, 1) * 100):.2f}%",
        }

    def log_summary(self) -> None:
        """Log a summary of streaming statistics."""
        avg_time_to_first_delta_ms = f"{self.get_avg_time_to_first_delta_ms():.2f}"
        avg_time_to_first_visible_ms = (
            f"{self.get_avg_time_to_first_visible_ms():.2f}"
        )
        model_stats = {
            model: self._summarize_model(stats)
            for model, stats in self.model_stats.items()
        }
        logger.info(
            "streaming_stats_summary",
            extra={
                "total_attempts": self.total_attempts,
                "successful_streams": self.successful_streams,
                "fallback_to_complete": self.fallback_to_complete,
                "success_rate": f"{self.get_success_rate() * 100:.2f}%",
                "fallback_rate": f"{self.get_fallback_rate() * 100:.2f}%",
                "total_deltas": self.total_deltas,
                "delta_failures": self.delta_failures,
                "final_message_failures": self.final_message_failures,
                "avg_time_to_first_delta_ms": avg_time_to_first_delta_ms,
                "avg_time_to_first_visible_ms": avg_time_to_first_visible_ms,
                "avg_stream_duration_ms": f"{self.get_avg_stream_duration_ms():.2f}",
                "model_stats": model_stats,
            },
        )


class StreamingMetricsCollector:
    """Global streaming metrics collector (singleton)."""

    _instance: StreamingStats | None = None
    _lock = False

    @classmethod
    def get_instance(cls) -> StreamingStats:
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = StreamingStats()
        return cls._instance

    @classmethod
    def record_streaming_attempt(cls, model: str | None = None) -> None:
        """Record a streaming attempt."""
        cls.get_instance().record_attempt(model)

    @classmethod
    def record_streaming_success(cls, model: str | None = None) -> None:
        """Record a successful streaming session."""
        cls.get_instance().record_success(model)

    @classmethod
    def record_streaming_fallback(cls, model: str | None = None) -> None:
        """Record a fallback to non-streaming."""
        cls.get_instance().record_fallback(model)

    @classmethod
    def record_delta(cls) -> None:
        """Record a delta delivery."""
        cls.get_instance().record_delta()

    @classmethod
    def record_delta_failure(cls) -> None:
        """Record a delta delivery failure."""
        cls.get_instance().record_delta_failure()

    @classmethod
    def record_final_failure(cls) -> None:
        """Record a final message delivery failure."""
        cls.get_instance().record_final_failure()


# Convenience functions for metrics recording
def record_streaming_attempt(model: str | None = None) -> None:
    """Record a streaming attempt."""
    StreamingMetricsCollector.record_streaming_attempt(model)


def record_streaming_success(model: str | None = None) -> None:
    """Record a successful streaming session."""
    StreamingMetricsCollector.record_streaming_success(model)


def record_streaming_fallback(model: str | None = None) -> None:
    """Record a fallback to non-streaming."""
    StreamingMetricsCollector.record_streaming_fallback(model)


def record_delta_delivery() -> None:
    """Record a delta delivery."""
    StreamingMetricsCollector.record_delta()


def record_delta_failure() -> None:
    """Record a delta delivery failure."""
    StreamingMetricsCollector.record_delta_failure()


def record_final_failure() -> None:
    """Record a final message delivery failure."""
    StreamingMetricsCollector.record_final_failure()


__all__ = [
    "StreamingStats",
    "StreamingMetricsCollector",
    "record_streaming_attempt",
    "record_streaming_success",
    "record_streaming_fallback",
    "record_delta_delivery",
    "record_delta_failure",
    "record_final_failure",
]
