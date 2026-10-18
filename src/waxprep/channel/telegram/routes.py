"""FastAPI routes for Telegram webhooks."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request

from waxprep.channel.contracts import NormalizedInbound
from waxprep.channel.telegram.normalize import normalize_telegram_update
from waxprep.channel.whatsapp.handler import InboundResult
from waxprep.domain.identifiers import WaxId

logger = logging.getLogger(__name__)


def build_telegram_router(
    *,
    secret_token: str | None,
    resolve_sender: Callable[[str], WaxId | None],
    accept_inbound: Callable[
        [NormalizedInbound], InboundResult | None | Awaitable[InboundResult | None]
    ],
) -> APIRouter:
    router = APIRouter(prefix="/channels/telegram", tags=["telegram"])

    @router.post("/webhook")
    async def webhook_receive(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(
            None, alias="X-Telegram-Bot-Api-Secret-Token"
        ),
    ) -> dict[str, Any]:
        if secret_token:
            if x_telegram_bot_api_secret_token != secret_token:
                raise HTTPException(status_code=403, detail="invalid secret token")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc

        messages = normalize_telegram_update(payload)
        results: list[dict[str, Any]] = []
        for msg in messages:
            wax = resolve_sender(msg.provider_sender_id)
            if wax is None:
                logger.info(
                    "unresolved telegram sender",
                    extra={"sender": msg.provider_sender_id},
                )
                continue
            resolved = replace(msg, wax_id=wax)
            result = accept_inbound(resolved)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
            results.append(
                {
                    "provider_message_id": msg.provider_message_id,
                    "accepted": result.accepted,
                    "wax_id": str(result.wax_id) if result.wax_id else None,
                    "error": result.error,
                }
            )
        return {
            "status": "ok",
            "message_count": len(messages),
            "processed_count": len(results),
            "results": results,
        }

    return router


__all__ = ["build_telegram_router"]
