"""HTTP application entry point for Wax Prep."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from waxprep import __version__
from waxprep.channel.contracts import NormalizedInbound
from waxprep.channel.whatsapp.handler import (
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
    InboundResult,
)
from waxprep.channel.whatsapp.outbox import WhatsAppOutbox, OutboxConfig
from waxprep.clock import SystemClock
from waxprep.settings import load_settings
from waxprep.storage.protocol import Storage
from waxprep.wiring import build_services

logger = logging.getLogger(__name__)

app = FastAPI(title="Wax Prep", version=__version__)

_services: Storage | None = None
_turn_service: Any = None
_history: Any = None
_clock: Any = None
_outbox: WhatsAppOutbox | None = None


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Report whether the application process can serve core dependencies."""
    status: dict[str, str] = {
        "status": "ok",
        "service": "waxprep",
        "version": __version__,
    }
    if _services is None:
        status["status"] = "degraded"
        status["reason"] = "services not composed"
        return status
    try:
        # Probe storage reachability: a get_student call exercises the
        # storage round trip (RLS, connection pool, encryption setup)
        # without mutating anything.
        from waxprep.domain.identifiers import new_wax_id

        _services.get_student(new_wax_id())
    except Exception as exc:  # noqa: BLE001 — health must never crash
        logger.exception("health check failed: storage unreachable")
        status["status"] = "degraded"
        status["reason"] = f"storage unreachable: {type(exc).__name__}"
    return status


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Compose services, start workers, and shut down cleanly."""
    global _services, _turn_service, _history, _clock, _outbox

    if os.environ.get("WAXPREP_WHATSAPP_ENABLED", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        yield
        return

    try:
        settings = load_settings()
        if not settings.whatsapp_enabled:
            yield
            return

        services = build_services(
            storage=services_storage(),
            cipher=build_cipher(),
            clock=SystemClock(),
            context_model=build_context_model(),
            teacher=None,
            gateway=build_gateway(settings),
        )
        _services = services.storage
        _turn_service = services.turns
        _history = services.history
        _clock = SystemClock()

        from waxprep.channel.whatsapp.routes import build_whatsapp_router

        def resolve_sender(sender: str):
            identity = _services.get_channel_identity("whatsapp", sender)
            return identity.wax_id if identity is not None else None

        async def accept_inbound(msg: NormalizedInbound) -> InboundResult | None:
            handler = WhatsAppInboundHandler(
                storage=_services,
                history=_history,
                turn_service=_turn_service,
                clock=_clock,
                config=WhatsAppInboundConfig(),
            )
            return await handler.handle_message(msg)

        router = build_whatsapp_router(
            verify_token=settings.whatsapp_verify_token or "",
            app_secret=settings.whatsapp_app_secret or "",
            resolve_sender=resolve_sender,
            accept_inbound=accept_inbound,
        )
        app.include_router(router)

        client = build_whatsapp_client(settings)
        _outbox = WhatsAppOutbox(
            storage=_services,
            client=client,
            clock=_clock,
            config=OutboxConfig(),
        )
        asyncio.create_task(_outbox.start())

        logger.info("whatsapp channel mounted and outbox worker started")
    except Exception:
        logger.exception("failed to compose whatsapp services")
    yield

    if _outbox is not None:
        try:
            await _outbox.stop()
        except Exception:
            logger.exception("error stopping outbox worker")


app.router.lifespan_context = lifespan


def services_storage() -> Storage:
    """Build the production storage backend."""
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        from waxprep.storage.postgres import PostgresStorage

        return PostgresStorage(database_url)
    from waxprep.storage.memory import InMemoryStorage

    return InMemoryStorage()


def build_cipher():
    from waxprep.security.cipher import FieldCipher

    return FieldCipher()


def build_context_model():
    from waxprep.context.model import ContextModel

    return ContextModel()


def build_gateway(settings):
    from waxprep.model_gateway.gateway import ModelGateway

    return ModelGateway(
        api_key=settings.upstage_api_key,
        base_url=settings.upstage_base_url,
    )


def build_whatsapp_client(settings):
    from waxprep.channel.whatsapp.client import WhatsAppClient

    return WhatsAppClient(
        token=settings.whatsapp_token or "",
        phone_number_id=settings.whatsapp_phone_number_id or "",
        api_version=settings.whatsapp_api_version,
    )
