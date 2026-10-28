"""Live tests for streaming with real Telegram and Upstage APIs.

These tests are gated behind the ``-m live`` marker. They require:
- WAXPREP_RUN_LIVE_TESTS=1
- TELEGRAM_BOT_TOKEN (the bot token)
- TELEGRAM_CHAT_ID (a real chat ID to send to)
- UPSTAGE_API_KEY (the Upstage API key)

Run with:
    WAXPREP_RUN_LIVE_TESTS=1 TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
    UPSTAGE_API_KEY=... uv run pytest -m live --tb=short
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.live

_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_UPSTAGE_API_KEY = os.environ.get("UPSTAGE_API_KEY", "")


def _live_enabled() -> bool:
    return (
        os.environ.get("WAXPREP_RUN_LIVE_TESTS") == "1"
        and bool(_TELEGRAM_BOT_TOKEN)
        and bool(_UPSTAGE_API_KEY)
    )


# ---------------------------------------------------------------------------
# Telegram Bot API verification (blueprint §5, §13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_bot_exists() -> None:
    """Verify the Telegram bot is reachable via getMe."""
    if not _live_enabled() or not _TELEGRAM_BOT_TOKEN:
        pytest.skip("live tests not enabled")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/getMe"
        )
        data = resp.json()
    assert data["ok"], f"getMe failed: {data}"
    assert data["result"]["is_bot"]


@pytest.mark.asyncio
async def test_telegram_send_message_draft_method_exists() -> None:
    """Verify that sendMessageDraft is a recognized Telegram Bot API method.

    Blueprint §5: the method must exist before we implement native
    draft streaming. If the method does not exist, Telegram returns a
    404 or 'method not found' error. If it exists but the chat_id is
    invalid, it returns 'chat not found'.
    """
    if not _live_enabled() or not _TELEGRAM_BOT_TOKEN:
        pytest.skip("live tests not enabled")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessageDraft",
            json={
                "chat_id": "0",  # invalid chat — just checking the method exists
                "text": "test",
                "draft_id": str(uuid4()),
            },
        )
        data = resp.json()
    # If the method exists, we get a 400 "chat not found" (the chat_id=0 is
    # invalid). If the method doesn't exist, we'd get a 404 or a different
    # error message.
    assert resp.status_code == 400, (
        f"sendMessageDraft returned status {resp.status_code}; "
        f"expected 400 (chat not found) if the method exists. "
        f"Response: {data}"
    )
    assert "chat" in data.get("description", "").lower(), (
        f"sendMessageDraft returned: {data}. If the method didn't exist, "
        "Telegram would return a different error."
    )


@pytest.mark.asyncio
async def test_telegram_native_draft_streaming_to_real_chat() -> None:
    """End-to-end: stream a draft to a real Telegram chat using
    sendMessageDraft, then send a permanent final message.

    Requires TELEGRAM_CHAT_ID to be set.
    """
    if not _live_enabled() or not _TELEGRAM_CHAT_ID:
        pytest.skip("TELEGRAM_CHAT_ID not set")

    from waxprep.channel.telegram.client import TelegramClient

    client = TelegramClient(bot_token=_TELEGRAM_BOT_TOKEN)
    # draft_id MUST be a non-zero integer per the Bot API.
    draft_id = uuid4().int & ((1 << 62) - 1) | 1
    try:
        # Send initial draft.
        result1 = await client.send_message_draft(
            chat_id=_TELEGRAM_CHAT_ID,
            text="Generating answer...",
            draft_id=draft_id,
        )
        assert result1.success, f"draft 1 failed: {result1.error_detail}"

        await asyncio.sleep(1)

        # Update the draft with more text.
        result2 = await client.send_message_draft(
            chat_id=_TELEGRAM_CHAT_ID,
            text="Generating answer... The answer is 42.",
            draft_id=draft_id,
        )
        assert result2.success, f"draft 2 failed: {result2.error_detail}"

        await asyncio.sleep(1)

        # Send the permanent final message.
        from waxprep.channel.contracts import OutboundPayload
        from waxprep.domain.identifiers import new_wax_id

        final = await client.send_text(
            OutboundPayload(
                channel="telegram",
                wax_id=new_wax_id(),
                recipient_external_id=_TELEGRAM_CHAT_ID,
                text="The answer is 42. (This is the permanent final message.)",
            )
        )
        assert final.success, f"final message failed: {final.error_detail}"
        assert final.provider_message_id is not None
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Upstage streaming verification (blueprint §2.1, §7.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upstage_streaming_produces_text_deltas() -> None:
    """Verify the Upstage Chat Completions API streams text deltas via SSE.

    Blueprint §2.1: the provider layer can stream. The orchestration
    layer should consume common events rather than depend directly on
    Upstage-specific response shapes.
    """
    if not _live_enabled() or not _UPSTAGE_API_KEY:
        pytest.skip("live tests not enabled")

    from waxprep.channel.streaming.accumulator import AnswerAccumulator

    acc = AnswerAccumulator()
    acc.start()

    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream(
            "POST",
            "https://api.upstage.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_UPSTAGE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "solar-pro",
                "messages": [
                    {"role": "user", "content": "Say hello in one short sentence."}
                ],
                "stream": True,
                "max_tokens": 50,
            },
        ) as resp:
            assert resp.status_code == 200
            async for line in aiter_response_lines(resp):
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                import json

                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        acc.append_delta(content)

    acc.complete()
    final_text = acc.final_text
    assert final_text, "no text was streamed"
    assert len(final_text) > 0
    # The answer should contain "hello" (case-insensitive) — it's a
    # reasonable semantic check that the text is an actual answer.
    assert "hello" in final_text.lower() or "hi" in final_text.lower(), (
        f"expected hello/hi in the streamed answer; got: {final_text!r}"
    )


async def aiter_response_lines(resp: httpx.Response):
    """Async iterator over lines in an SSE response."""
    async for line in resp.aiter_lines():
        yield line
