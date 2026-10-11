"""FastAPI routes for WhatsApp webhooks.

Fast path: verify → normalize → durable accept → 200.
No model work inside the webhook request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from waxprep.channel.whatsapp.normalize import normalize_webhook_payload
from waxprep.channel.whatsapp.security import verify_signature, verify_webhook_challenge
from waxprep.domain.identifiers import WaxId

logger = logging.getLogger(__name__)


def build_whatsapp_router(
    *,
    verify_token: str,
    app_secret: str,
    resolve_sender: Callable[[str], WaxId | None],
    accept_inbound: Callable[[Any], None],
) -> APIRouter:
    router = APIRouter(prefix="/channels/whatsapp", tags=["whatsapp"])

    @router.get("/webhook")
    def webhook_verify(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(None, alias="hub.challenge"),
    ) -> Response:
        challenge = verify_webhook_challenge(
            hub_mode, hub_verify_token, hub_challenge, verify_token
        )
        if challenge is None:
            raise HTTPException(status_code=403, detail="verification failed")
        return Response(content=challenge, media_type="text/plain")

    @router.post("/webhook")
    async def webhook_receive(
        request: Request,
        x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    ) -> dict[str, str]:
        body = await request.body()
        if not verify_signature(app_secret, body, x_hub_signature_256):
            raise HTTPException(status_code=403, detail="invalid signature")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc

        # Resolve map built per message
        def _resolve(sender: str) -> WaxId | None:
            return resolve_sender(sender)

        # normalize needs dict mapping — build lazily inside by re-normalizing
        # with a flat map: we pass None and resolve after
        messages = normalize_webhook_payload(payload, resolve_wax_id=None)
        for msg in messages:
            wax = resolve_sender(msg.provider_sender_id)
            # rebuild with wax id
            from dataclasses import replace

            resolved = replace(msg, wax_id=wax)
            if wax is None:
                logger.info(
                    "unresolved whatsapp sender ignored",
                    extra={"sender": msg.provider_sender_id},
                )
                continue
            accept_inbound(resolved)
        return {"status": "accepted"}

    return router
