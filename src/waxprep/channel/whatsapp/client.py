"""Authenticated WhatsApp Cloud API client (media + outbound text)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from waxprep.channel.contracts import OutboundPayload, OutboundResult

logger = logging.getLogger(__name__)


class WhatsAppClient:
    def __init__(
        self,
        token: str,
        phone_number_id: str,
        *,
        api_version: str = "v21.0",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        self._client = client
        self._base = f"https://graph.facebook.com/{api_version}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        """Download media via Graph API (never trust webhook URLs)."""
        close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=60.0)
            close = True
        try:
            meta = await client.get(f"{self._base}/{media_id}", headers=self._headers())
            meta.raise_for_status()
            url = meta.json().get("url")
            mime = meta.json().get("mime_type")
            if not url:
                raise RuntimeError("media url missing")
            data_resp = await client.get(url, headers=self._headers())
            data_resp.raise_for_status()
            return data_resp.content, mime
        finally:
            if close:
                await client.aclose()

    async def send_text(self, payload: OutboundPayload) -> OutboundResult:
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": payload.recipient_external_id,
            "type": "text",
            "text": {"body": payload.text or ""},
        }
        close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
            close = True
        try:
            resp = await client.post(
                f"{self._base}/{self._phone_number_id}/messages",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code == 429:
                # WhatsApp rate limit: temporary failure with Retry-After.
                retry_after = resp.headers.get("Retry-After")
                retry_at = None
                if retry_after:
                    try:
                        from datetime import UTC, datetime, timedelta

                        retry_at = datetime.now(tz=UTC) + timedelta(
                            seconds=int(retry_after)
                        )
                    except (TypeError, ValueError):
                        pass
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=True,
                    error_code="429",
                    error_detail="provider rate limited; retry after backoff",
                )
            if resp.status_code >= 500:
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=True,
                    error_code=str(resp.status_code),
                    error_detail="provider_5xx",
                )
            if resp.status_code >= 400:
                return OutboundResult(
                    success=False,
                    provider_message_id=None,
                    temporary_failure=False,
                    error_code=str(resp.status_code),
                    error_detail=resp.text[:200],
                )
            data = resp.json()
            messages = data.get("messages") or []
            mid = messages[0].get("id") if messages else None
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
                error_detail="request timed out; delivery outcome uncertain",
            )
        finally:
            if close:
                await client.aclose()

    async def send_typing(self, recipient_external_id: str) -> None:
        """Best-effort typing indicator.

        WhatsApp Cloud API typing indicators are channel-level UX. This
        method is a safe no-op hook so progressive delivery can call it
        without educational branching. Override or extend when the Meta
        presence API is configured for the phone number.
        """
        return
