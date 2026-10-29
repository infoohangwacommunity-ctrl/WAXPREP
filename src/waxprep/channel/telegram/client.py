"""Telegram Bot API client (sendMessage, typing)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from waxprep.channel.contracts import OutboundPayload, OutboundResult

logger = logging.getLogger(__name__)


class TelegramClient:
    """Minimal Telegram Bot API client for text tutoring replies."""

    def __init__(
        self,
        bot_token: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = bot_token
        self._base = api_base.rstrip("/")
        self._timeout = timeout
        self._client = http_client
        self._owns_client = http_client is None

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_text(
        self,
        payload: OutboundPayload,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> OutboundResult:
        """Send a plain-text message (multi-message handled by caller).

        ``reply_markup`` may carry an InlineKeyboardMarkup dict for
        interactive next-step buttons (Bot API ``InlineKeyboardButton``).
        """
        text = (payload.text or "").strip()
        if not text:
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=False,
                error_code="empty",
                error_detail="empty text",
            )
        client = await self._http()
        body: dict[str, Any] = {
            "chat_id": payload.recipient_external_id,
            "text": text,
            # Prefer plain text for tutoring clarity; HTML optional later.
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        try:
            resp = await client.post(self._url("sendMessage"), json=body)
            if resp.status_code == 429:
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=True,
                    error_code="429",
                    error_detail="rate limited",
                )
            if resp.status_code >= 500:
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=True,
                    error_code=str(resp.status_code),
                    error_detail="telegram_5xx",
                )
            data = resp.json()
            if not data.get("ok"):
                desc = str(data.get("description") or resp.text)[:200]
                # 400 often permanent (bad chat id); 403 blocked bot
                temp = resp.status_code >= 500
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=temp,
                    error_code=str(data.get("error_code") or resp.status_code),
                    error_detail=desc,
                )
            result = data.get("result") or {}
            mid = (
                str(result.get("message_id"))
                if result.get("message_id") is not None
                else None
            )
            return OutboundResult(
                success=True,
                provider_message_id=mid,
                temporary_failure=False,
            )
        except httpx.TimeoutException:
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=True,
                error_code="timeout",
                error_detail="telegram timeout",
            )
        except Exception as exc:
            logger.exception("telegram send_text failed")
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=True,
                error_code="exception",
                error_detail=type(exc).__name__,
            )

    async def send_typing(self, chat_id: str) -> None:
        client = await self._http()
        try:
            await client.post(
                self._url("sendChatAction"),
                json={"chat_id": chat_id, "action": "typing"},
            )
        except Exception:
            logger.debug("telegram typing failed", exc_info=True)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> None:
        """Acknowledge a callback query (best-effort).

        Telegram REQUIRES answering every callback query, ideally quickly:
        until answered the client shows a loading spinner. Best-effort —
        acknowledgement must never block or fail the student's turn.
        """
        client = await self._http()
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            body["text"] = text[:200]
        try:
            await client.post(self._url("answerCallbackQuery"), json=body)
        except Exception:
            logger.debug("telegram answer_callback_query failed", exc_info=True)

    async def send_message_draft(
        self,
        *,
        chat_id: str,
        text: str,
        draft_id: int,
        thread_id: int | None = None,
    ) -> OutboundResult:
        """Send or update a native live-draft message (Telegram Bot API
        ``sendMessageDraft``).

        Per the official Bot API: ``draft_id`` must be a NON-ZERO INTEGER
        identifying the draft; reusing the same id updates the existing
        draft (animated preview), a different id creates a new one. The
        draft is an ephemeral ~30-second preview in the chat and is NOT a
        message — a permanent ``sendMessage`` must finalize the reply once
        generation finishes. The API returns ``True`` and carries no
        message id, so ``provider_message_id`` stays None on success.

        Introduced in Bot API 9.3 (2025-12-31), opened to all bots in
        Bot API 9.5 (2026-03-01); Bot API 10.0 allows empty text (show a
        placeholder while the first tokens are pending).

        Blueprint §5: the same ``draft_id`` must be reused for updates
        to the same response.
        """
        client = await self._http()
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "draft_id": draft_id,
        }
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        try:
            resp = await client.post(self._url("sendMessageDraft"), json=body)
            if resp.status_code == 429:
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=True,
                    error_code="429",
                    error_detail="rate limited",
                )
            data = resp.json()
            if not data.get("ok"):
                desc = str(data.get("description") or resp.text)[:200]
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=resp.status_code >= 500,
                    error_code=str(data.get("error_code") or resp.status_code),
                    error_detail=desc,
                )
            # Bot API returns result=true for drafts (boolean), not a message object.
            result = data.get("result")
            mid = None
            if isinstance(result, dict) and result.get("message_id") is not None:
                mid = str(result.get("message_id"))
            return OutboundResult(
                success=True,
                provider_message_id=mid,
                temporary_failure=False,
            )
        except httpx.TimeoutException:
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=True,
                error_code="timeout",
                error_detail="telegram draft timeout",
            )
        except Exception as exc:
            logger.exception("telegram send_message_draft failed")
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=True,
                error_code="exception",
                error_detail=type(exc).__name__,
            )

    async def set_webhook(
        self, url: str, *, secret_token: str | None = None
    ) -> dict[str, Any]:
        client = await self._http()
        body: dict[str, Any] = {
            "url": url,
            # callback_query updates MUST be requested explicitly, otherwise
            # Telegram never delivers button clicks to this webhook.
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        }
        if secret_token:
            body["secret_token"] = secret_token
        resp = await client.post(self._url("setWebhook"), json=body)
        return resp.json()

    async def get_webhook_info(self) -> dict[str, Any]:
        client = await self._http()
        resp = await client.get(self._url("getWebhookInfo"))
        return resp.json()
