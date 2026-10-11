"""HTTP application entry point for Wax Prep."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from waxprep import __version__

logger = logging.getLogger(__name__)

app = FastAPI(title="Wax Prep", version=__version__)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Report whether the application process is running."""
    return {
        "status": "ok",
        "service": "waxprep",
        "version": __version__,
    }


def _try_mount_whatsapp() -> None:
    if os.environ.get("WAXPREP_WHATSAPP_ENABLED", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return
    try:
        from waxprep.channel.whatsapp.routes import build_whatsapp_router
        from waxprep.settings import load_settings

        settings = load_settings()
        if not settings.whatsapp_enabled:
            return

        def resolve_sender(sender: str):  # type: ignore[no-untyped-def]
            return None  # identity resolution wired at composition root

        def accept_inbound(msg):  # type: ignore[no-untyped-def]
            logger.info(
                "whatsapp inbound accepted",
                extra={"provider_message_id": msg.provider_message_id},
            )

        router = build_whatsapp_router(
            verify_token=settings.whatsapp_verify_token or "",
            app_secret=settings.whatsapp_app_secret or "",
            resolve_sender=resolve_sender,
            accept_inbound=accept_inbound,
        )
        app.include_router(router)
    except Exception:
        logger.exception("failed to mount WhatsApp routes")


_try_mount_whatsapp()
