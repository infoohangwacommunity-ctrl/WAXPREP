"""Intelligent turn assembly: lifecycle, deadline, semantic grouping.

The time window stays the default (and the fallback): these tests pin
the resettable quiet period's hard collection deadline, the explicit
turn lifecycle, and the opt-in small-model semantic grouper — including
its failure fallbacks, so a model problem can never stall a student's
turn. Texts stay content-free wherever the time window should decide.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from waxprep.channel.contracts import NormalizedInbound
from waxprep.channel.message_queue import (
    GroupingDecision,
    MessageQueue,
    ModelSemanticGrouper,
    QueueConfig,
    QueuedMessage,
    TurnPhase,
    merge_batch_text,
    parse_grouping_decision,
)
from waxprep.clock import FakeClock
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.model_gateway.models import ModelRole


def _msg(text: str | None) -> NormalizedInbound:
    return NormalizedInbound(
        channel="whatsapp",
        wax_id=None,
        provider_message_id=f"wamid.{uuid4()}",
        provider_sender_id="15551234567",
        text=text,
        occurred_at=datetime(2026, 10, 28, 12, 0, tzinfo=UTC),
        content_kind="text",
        attachments=(),
        structured={},
        reply_to_provider_id=None,
    )


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 28, 12, 0, tzinfo=UTC))


class _RecordingGrouper:
    """Grouper double that replays scripted decisions and records calls."""

    def __init__(self, *decisions: GroupingDecision) -> None:
        self.decisions = list(decisions)
        self.calls: list[tuple[str, ...]] = []

    async def classify(
        self,
        wax_id: WaxId,
        pending_texts: tuple[str, ...],
        arrival_text: str,
    ) -> GroupingDecision:
        self.calls.append(pending_texts + (arrival_text,))
        if not self.decisions:
            return GroupingDecision.UNCERTAIN
        return self.decisions.pop(0)


class _ExplodingGrouper:
    """Grouper double that always fails — the queue must fall back."""

    async def classify(
        self,
        wax_id: WaxId,
        pending_texts: tuple[str, ...],
        arrival_text: str,
    ) -> GroupingDecision:
        raise RuntimeError("grouping model down")


class _Runner:
    """Turn runner double recording each batch's texts."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def __call__(self, wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        self.batches.append([(item.message.text or "") for item in batch])


class _BlockingRunner(_Runner):
    """Runner that holds the turn open until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def __call__(self, wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        await super().__call__(wax_id, batch)
        await self.release.wait()


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def _submit(queue: MessageQueue, wax: WaxId, text: str) -> None:
    assert await queue.submit(_msg(text), wax, channel="whatsapp") is not None


@pytest.mark.asyncio
async def test_hard_deadline_ends_collection_despite_long_window() -> None:
    # The quiet period never elapses (window 30s) — only the hard
    # collection deadline can start the turn.
    config = QueueConfig(
        coalesce_window_seconds=30.0,
        max_collection_seconds=0.15,
        worker_idle_timeout_seconds=1.0,
    )
    runner = _Runner()
    queue = MessageQueue(_clock(), config, turn_runner=runner)
    wax = new_wax_id()
    await _submit(queue, wax, "part one")
    await _submit(queue, wax, "part two")

    await _wait_until(lambda: len(runner.batches) >= 1)
    await queue.stop()

    assert len(runner.batches) == 1
    assert runner.batches[0] == ["part one", "part two"]


@pytest.mark.asyncio
async def test_new_request_splits_batch_and_arrival_opens_next_turn() -> None:
    grouper = _RecordingGrouper(GroupingDecision.NEW_REQUEST)
    config = QueueConfig(
        coalesce_window_seconds=30.0,
        max_collection_seconds=0.3,
        semantic_grouping_enabled=True,
        grouping_timeout_seconds=1.0,
        worker_idle_timeout_seconds=1.0,
    )
    runner = _Runner()
    queue = MessageQueue(_clock(), config, turn_runner=runner, semantic_grouper=grouper)
    wax = new_wax_id()
    await _submit(queue, wax, "explain photosynthesis")
    await _submit(queue, wax, "actually, new question")

    await _wait_until(lambda: len(runner.batches) >= 1)
    assert runner.batches[0] == ["explain photosynthesis"]
    assert grouper.calls == [("explain photosynthesis", "actually, new question")]

    # The rejected arrival stays pending and opens the next turn.
    await _wait_until(lambda: len(runner.batches) >= 2)
    await queue.stop()

    assert runner.batches[1] == ["actually, new question"]


@pytest.mark.asyncio
async def test_join_decision_keeps_collecting_into_one_turn() -> None:
    grouper = _RecordingGrouper(GroupingDecision.JOIN)
    config = QueueConfig(
        coalesce_window_seconds=30.0,
        max_collection_seconds=0.15,
        semantic_grouping_enabled=True,
        grouping_timeout_seconds=1.0,
        worker_idle_timeout_seconds=1.0,
    )
    runner = _Runner()
    queue = MessageQueue(_clock(), config, turn_runner=runner, semantic_grouper=grouper)
    wax = new_wax_id()
    await _submit(queue, wax, "part one")
    await _submit(queue, wax, "part two")

    await _wait_until(lambda: len(runner.batches) >= 1)
    await queue.stop()

    assert len(runner.batches) == 1
    assert runner.batches[0] == ["part one", "part two"]


@pytest.mark.asyncio
async def test_grouping_disabled_ignores_wired_grouper() -> None:
    grouper = _RecordingGrouper(GroupingDecision.NEW_REQUEST)
    config = QueueConfig(
        coalesce_window_seconds=0.05,
        max_collection_seconds=1.0,
        semantic_grouping_enabled=False,
        worker_idle_timeout_seconds=1.0,
    )
    runner = _Runner()
    queue = MessageQueue(_clock(), config, turn_runner=runner, semantic_grouper=grouper)
    wax = new_wax_id()
    await _submit(queue, wax, "part one")
    await _submit(queue, wax, "part two")

    await _wait_until(lambda: len(runner.batches) >= 1)
    await queue.stop()

    assert grouper.calls == []
    assert len(runner.batches) == 1
    assert runner.batches[0] == ["part one", "part two"]


@pytest.mark.asyncio
async def test_exploding_grouper_falls_back_to_time_window() -> None:
    config = QueueConfig(
        coalesce_window_seconds=0.05,
        max_collection_seconds=1.0,
        semantic_grouping_enabled=True,
        grouping_timeout_seconds=0.2,
        worker_idle_timeout_seconds=1.0,
    )
    runner = _Runner()
    queue = MessageQueue(
        _clock(), config, turn_runner=runner, semantic_grouper=_ExplodingGrouper()
    )
    wax = new_wax_id()
    await _submit(queue, wax, "part one")
    await _submit(queue, wax, "part two")

    await _wait_until(lambda: len(runner.batches) >= 1)
    await queue.stop()

    # The model failing must never stall the turn: the time window
    # decided, and both messages joined one batch.
    assert len(runner.batches) == 1
    assert runner.batches[0] == ["part one", "part two"]


@pytest.mark.asyncio
async def test_turn_phase_lifecycle_transitions() -> None:
    # A long window keeps the turn in COLLECTING for a full second so
    # the phase is observable before the hard deadline starts the turn.
    config = QueueConfig(
        coalesce_window_seconds=30.0,
        max_collection_seconds=1.0,
        worker_idle_timeout_seconds=1.0,
    )
    runner = _BlockingRunner()
    queue = MessageQueue(_clock(), config, turn_runner=runner)
    wax = new_wax_id()

    assert queue.get_phase(wax) is None
    await _submit(queue, wax, "hello")

    await _wait_until(lambda: queue.get_phase(wax) is TurnPhase.COLLECTING)
    assert queue.get_phase(wax) is TurnPhase.COLLECTING

    await _wait_until(lambda: queue.get_phase(wax) is TurnPhase.PROCESSING)
    assert queue.get_phase(wax) is TurnPhase.PROCESSING
    assert len(runner.batches) == 1

    runner.release.set()
    await _wait_until(lambda: queue.get_phase(wax) is TurnPhase.COMPLETED)
    await queue.stop()

    assert queue.get_phase(wax) is TurnPhase.COMPLETED


@pytest.mark.asyncio
async def test_follow_up_arrival_while_runner_blocked_starts_next_turn() -> None:
    config = QueueConfig(
        coalesce_window_seconds=0.05,
        max_collection_seconds=0.2,
        worker_idle_timeout_seconds=1.0,
    )
    runner = _BlockingRunner()
    queue = MessageQueue(_clock(), config, turn_runner=runner)
    wax = new_wax_id()
    await _submit(queue, wax, "first")

    await _wait_until(lambda: queue.get_phase(wax) is TurnPhase.PROCESSING)
    # A follow-up arrives while the first turn is still running.
    await _submit(queue, wax, "second")
    assert queue.get_queue_length(wax) == 1

    runner.release.set()
    await _wait_until(lambda: len(runner.batches) >= 2)
    await queue.stop()

    assert runner.batches == [["first"], ["second"]]


def test_parse_grouping_decision_lenient_word_match() -> None:
    assert parse_grouping_decision("JOIN") is GroupingDecision.JOIN
    assert parse_grouping_decision(" Join.") is GroupingDecision.JOIN
    assert parse_grouping_decision("please continue") is GroupingDecision.JOIN
    assert parse_grouping_decision("new_request") is GroupingDecision.NEW_REQUEST
    assert parse_grouping_decision("New topic") is GroupingDecision.NEW_REQUEST
    assert parse_grouping_decision("") is GroupingDecision.UNCERTAIN
    assert parse_grouping_decision(None) is GroupingDecision.UNCERTAIN
    assert parse_grouping_decision("banana") is GroupingDecision.UNCERTAIN


def test_merge_batch_text_skips_blank_parts() -> None:
    wax = new_wax_id()
    batch = [
        QueuedMessage(id=uuid4(), wax_id=wax, message=_msg("first"), received_at=0.0),
        QueuedMessage(id=uuid4(), wax_id=wax, message=_msg(None), received_at=0.1),
        QueuedMessage(id=uuid4(), wax_id=wax, message=_msg("   "), received_at=0.2),
        QueuedMessage(id=uuid4(), wax_id=wax, message=_msg("last"), received_at=0.3),
    ]
    assert merge_batch_text(batch) == "first\n\nlast"


class _FakeGateway:
    """Gateway double recording requests with a scripted reply."""

    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> Any:
        self.requests.append(request)
        if self.reply is None:
            raise RuntimeError("gateway down")
        return SimpleNamespace(text=self.reply)


@pytest.mark.asyncio
async def test_model_grouper_maps_gateway_request_and_parses_reply() -> None:
    gateway = _FakeGateway(" JOIN ")
    grouper = ModelSemanticGrouper(gateway)  # type: ignore[arg-type]
    decision = await grouper.classify(
        new_wax_id(), ("explain photosynthesis",), "for jss2 please"
    )

    assert decision is GroupingDecision.JOIN
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.role is ModelRole.GROUPING
    assert request.model == "grouping"


@pytest.mark.asyncio
async def test_model_grouper_empty_arrival_is_join_without_gateway_call() -> None:
    gateway = _FakeGateway("new")
    grouper = ModelSemanticGrouper(gateway)  # type: ignore[arg-type]
    decision = await grouper.classify(new_wax_id(), ("explain",), "   ")

    assert decision is GroupingDecision.JOIN
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_model_grouper_gateway_failure_is_uncertain() -> None:
    gateway = _FakeGateway(None)
    grouper = ModelSemanticGrouper(gateway)  # type: ignore[arg-type]
    decision = await grouper.classify(
        new_wax_id(), ("explain photosynthesis",), "now something else"
    )

    assert decision is GroupingDecision.UNCERTAIN
