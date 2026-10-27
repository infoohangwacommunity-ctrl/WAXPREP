"""Per-student message queue: coalescing, FIFO order, rate limit, stop.

Grouping is purely arrival-time based. These tests deliberately use
content-free texts ("part 0", "part 1") to prove the queue never
inspects message content — no keyword matching decides the batch.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from waxprep.channel.contracts import NormalizedInbound
from waxprep.channel.message_queue import (
    MessageQueue,
    QueueConfig,
    QueuedMessage,
)
from waxprep.clock import FakeClock
from waxprep.domain.identifiers import WaxId, new_wax_id

FAST = QueueConfig(
    coalesce_window_seconds=0.05,
    worker_idle_timeout_seconds=1.0,
)


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 27, 10, 0, tzinfo=UTC))


def _msg(i: int) -> NormalizedInbound:
    return NormalizedInbound(
        channel="whatsapp",
        wax_id=None,
        provider_message_id=f"wamid.Q-{i}",
        provider_sender_id="15551234567",
        text=f"part {i}",
        occurred_at=datetime(2026, 10, 27, 10, 0, tzinfo=UTC),
        content_kind="text",
        attachments=(),
        structured={},
        reply_to_provider_id=None,
    )


@pytest.mark.asyncio
async def test_burst_coalesces_into_one_fifo_batch() -> None:
    batches: list[list[QueuedMessage]] = []
    done = asyncio.Event()

    async def runner(wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        batches.append(list(batch))
        done.set()

    queue = MessageQueue(_clock(), FAST, turn_runner=runner)
    wax = new_wax_id()
    ids = [await queue.submit(_msg(i), wax, channel="whatsapp") for i in range(3)]
    assert all(qid is not None for qid in ids)

    await asyncio.wait_for(done.wait(), timeout=5.0)
    await queue.stop()

    assert len(batches) == 1
    batch = batches[0]
    assert [item.message.text for item in batch] == ["part 0", "part 1", "part 2"]
    assert batch[-1].coalesced_with == [batch[0].id, batch[1].id]
    assert all(item.processed for item in batch)
    assert queue.get_queue_length(wax) == 0


@pytest.mark.asyncio
async def test_rate_limit_declines_extra_messages() -> None:
    seen: list[list[QueuedMessage]] = []
    done = asyncio.Event()

    async def runner(wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        seen.append(list(batch))
        done.set()

    config = QueueConfig(
        coalesce_window_seconds=0.05,
        worker_idle_timeout_seconds=1.0,
        rate_limit_per_student_per_second=2,
    )
    queue = MessageQueue(_clock(), config, turn_runner=runner)
    wax = new_wax_id()
    first = await queue.submit(_msg(10), wax, channel="whatsapp")
    second = await queue.submit(_msg(11), wax, channel="whatsapp")
    third = await queue.submit(_msg(12), wax, channel="whatsapp")
    assert first is not None
    assert second is not None
    assert third is None

    await asyncio.wait_for(done.wait(), timeout=5.0)
    await queue.stop()

    total = [item for batch in seen for item in batch]
    assert [item.message.text for item in total] == ["part 10", "part 11"]


@pytest.mark.asyncio
async def test_runner_failure_still_marks_processed() -> None:
    batches: list[list[QueuedMessage]] = []
    done = asyncio.Event()

    async def runner(wax_id: WaxId, batch: list[QueuedMessage]) -> None:
        batches.append(list(batch))
        done.set()
        raise RuntimeError("turn runner exploded")

    queue = MessageQueue(_clock(), FAST, turn_runner=runner)
    wax = new_wax_id()
    accepted = await queue.submit(_msg(30), wax, channel="whatsapp")
    assert accepted is not None

    await asyncio.wait_for(done.wait(), timeout=5.0)
    await queue.stop()

    assert len(batches) == 1
    assert all(item.processed for item in batches[0])


@pytest.mark.asyncio
async def test_stop_rejects_new_submissions() -> None:
    queue = MessageQueue(_clock(), FAST)
    wax = new_wax_id()
    accepted = await queue.submit(_msg(20), wax, channel="whatsapp")
    assert accepted is not None
    await queue.stop()
    declined = await queue.submit(_msg(21), wax, channel="whatsapp")
    assert declined is None
