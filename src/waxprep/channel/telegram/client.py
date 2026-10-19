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

    async def send_text(self, payload: OutboundPayload) -> OutboundResult:
        """Send a plain-text message (multi-message handled by caller)."""
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
        try:
            resp = await client.post(
                self._url("sendMessage"),
                json={
                    "chat_id": payload.recipient_external_id,
                    "text": text,
                    # Prefer plain text for tutoring clarity; HTML optional later.
                    "disable_web_page_preview": True,
                },
            )
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

    async def set_webhook(
        self, url: str, *, secret_token: str | None = None
    ) -> dict[str, Any]:
        client = await self._http()
        body: dict[str, Any] = {
            "url": url,
            "allowed_updates": ["message"],
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
