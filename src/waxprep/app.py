"""HTTP application entry point for Wax Prep."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from fastapi import FastAPI

from waxprep import __version__

if TYPE_CHECKING:
    from waxprep.channel.whatsapp.handler import WhatsAppInboundHandler

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


# Global handler instance - will be wired at composition root
handler: WhatsAppInboundHandler | None = None


def _try_mount_whatsapp() -> None:
    global handler
    if os.environ.get("WAXPREP_WHATSAPP_ENABLED", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return
    try:
        from waxprep.channel.whatsapp.routes import build_whatsapp_router
        from waxprep.channel.whatsapp.handler import (
            WhatsAppInboundHandler,
            WhatsAppInboundConfig,
        )
        from waxprep.settings import load_settings
        from waxprep.clock import SystemClock
        from waxprep.storage.memory import InMemoryStorage
        from waxprep.history.service import HistoryService
        from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst
        from waxprep.context.investigator import ContextInvestigator
        from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst
        from waxprep.model_gateway.gateway import ModelGateway
        from waxprep.index.indexer import Indexer

        settings = load_settings()
        if not settings.whatsapp_enabled:
            return

        # Initialize storage and services
        storage = InMemoryStorage()
        clock = SystemClock()
        indexer = Indexer(storage, clock)
        history = HistoryService(storage, indexer, clock)
        
        # Initialize model gateway and services
        gateway = ModelGateway()
        investigator = ContextInvestigator(gateway)
        teacher = ModelTeacherCiFirst(gateway)
        turn_service = TurnServiceCiFirst(
            storage=storage,
            history=history,
            investigator=investigator,
            teacher=teacher,
            clock=clock,
        )
        
        # Initialize WhatsApp inbound handler
        handler = WhatsAppInboundHandler(
            storage=storage,
            history=history,
            turn_service=turn_service,
            clock=clock,
            config=WhatsAppInboundConfig(),
        )

        def resolve_sender(sender: str):
            """Resolve WhatsApp sender to WAX ID."""
            return handler.resolve_sender(sender)

        async def accept_inbound(msg):
            """Durably accept and process inbound message."""
            result = await handler.handle_message(msg)
            logger.info(
                "whatsapp inbound processed",
                extra={
                    "provider_message_id": msg.provider_message_id,
                    "accepted": result.accepted,
                    "wax_id": str(result.wax_id) if result.wax_id else None,
                },
            )
            return result

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
