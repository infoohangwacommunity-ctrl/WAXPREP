"""Streaming infrastructure for WaxPrep."""

from waxprep.channel.streaming.accumulator import AnswerAccumulator
from waxprep.channel.streaming.contract import (
    ChannelCapabilities,
    ChannelCapability,
    DeliveryStrategy,
)
from waxprep.channel.streaming.coordinator import StreamingCoordinator
from waxprep.channel.streaming.formatter import (
    SafeIncrement,
    find_safe_boundary,
    is_safe_final,
    safe_increment,
)
from waxprep.channel.streaming.metrics import (
    StreamingMetricsCollector,
    StreamingStats,
    record_delta_delivery,
    record_delta_failure,
    record_final_failure,
    record_streaming_attempt,
    record_streaming_fallback,
    record_streaming_success,
)
from waxprep.channel.streaming.observability import (
    StreamingCounters,
    StreamingMetrics,
)
from waxprep.channel.streaming.pacing import PacingPolicy, TELEGRAM_DRAFT_DEFAULT, WHATSAPP_DEFAULT
from waxprep.channel.streaming.renderers import (
    telegram_capabilities,
    whatsapp_capabilities,
)

__all__ = [
    # Accumulator
    "AnswerAccumulator",
    # Contract
    "ChannelCapabilities",
    "ChannelCapability",
    "DeliveryStrategy",
    # Coordinator
    "StreamingCoordinator",
    # Formatter
    "SafeIncrement",
    "find_safe_boundary",
    "is_safe_final",
    "safe_increment",
    # Metrics
    "StreamingMetricsCollector",
    "StreamingStats",
    "record_streaming_attempt",
    "record_streaming_success",
    "record_streaming_fallback",
    "record_delta_delivery",
    "record_delta_failure",
    "record_final_failure",
    # Observability
    "StreamingCounters",
    "StreamingMetrics",
    # Pacing
    "PacingPolicy",
    "TELEGRAM_DRAFT_DEFAULT",
    "WHATSAPP_DEFAULT",
    # Renderers
    "telegram_capabilities",
    "whatsapp_capabilities",
]
