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
                pass  # retry_at unused in this path
                if retry_after:
                    try:
                        from datetime import UTC, datetime, timedelta

                        _retry_at = datetime.now(tz=UTC) + timedelta(
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

    async def send_interactive_buttons(
        self,
        *,
        recipient_external_id: str,
        body_text: str,
        buttons: list[dict[str, str]],
    ) -> OutboundResult:
        """Send a reply-button interactive message (max 3 buttons).

        Each button dict must have ``id`` and ``title``. Titles are truncated
        to Meta's 20-char limit. On provider rejection (e.g. outside 24h
        window), returns a permanent failure so the caller can fall back to
        plain text.
        """
        if not buttons:
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=False,
                error_code="no_buttons",
                error_detail="interactive send requires at least one button",
            )
        action_buttons = []
        for b in buttons[:3]:
            title = (b.get("title") or "")[:20]
            bid = b.get("id") or ""
            if not bid or not title:
                continue
            action_buttons.append(
                {"type": "reply", "reply": {"id": bid, "title": title}}
            )
        if not action_buttons:
            return OutboundResult(
                success=False,
                provider_message_id=None,
                temporary_failure=False,
                error_code="invalid_buttons",
                error_detail="no valid button id/title pairs",
            )
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": recipient_external_id,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text or ""},
                "action": {"buttons": action_buttons},
            },
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
        """Best-effort typing indicator via WhatsApp Cloud API.

        Blueprint §6.4: the previous implementation sent a "read" status
        with a fabricated ``message_id="typing-placeholder"`` and called it
        a typing indicator. A read receipt and a typing signal are different
        concepts. The fabricated placeholder has been removed.

        WhatsApp Cloud API does not currently document a general-purpose
        typing indicator for the Cloud API integration. If a supported
        typing mechanism is identified and verified, it should be implemented
        here and the capability reported via ``ChannelCapabilities``. Until
        then, this method is a documented no-op — it does not fabricate a
        request.
        """
        # No-op: WhatsApp Cloud API does not support a verified typing
        # indicator for this integration. Do not send a fabricated
        # placeholder message_id. The capability is reported as
        # unavailable via ChannelCapabilities.
        return
