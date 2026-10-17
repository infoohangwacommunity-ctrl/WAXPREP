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
    InboundResult,
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.channel.whatsapp.outbox import OutboxConfig, WhatsAppOutbox
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
    """Report process liveness. Composition is optional until lifespan runs."""
    status: dict[str, str] = {
        "status": "ok",
        "service": "waxprep",
        "version": __version__,
    }
    if _services is None:
        status["composition"] = "pending"
        return status
    try:
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

        cipher = build_cipher()
        storage = services_storage(cipher)
        clock = SystemClock()
        services = build_services(
            storage=storage,
            cipher=cipher,
            clock=clock,
            context_model=build_context_model(settings),
            teacher=None,
            gateway=build_gateway(settings),
        )
        # Bind locals so nested callbacks see non-None Storage
        active_storage: Storage = services.storage
        _services = active_storage
        _turn_service = services.turns
        _history = services.history
        _clock = clock

        from waxprep.channel.whatsapp.routes import build_whatsapp_router

        def resolve_sender(sender: str):
            identity = active_storage.get_channel_identity("whatsapp", sender)
            return identity.wax_id if identity is not None else None

        async def accept_inbound(msg: NormalizedInbound) -> InboundResult | None:
            handler = WhatsAppInboundHandler(
                storage=active_storage,
                history=services.history,
                turn_service=services.turns,
                clock=clock,
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
            storage=active_storage,
            client=client,
            clock=clock,
            config=OutboxConfig(),
        )
        asyncio.create_task(_outbox.start())

        # Background derivation worker (embeds, process_turn recovery, artifacts)
        async def _worker_loop() -> None:
            while True:
                try:
                    await services.worker.run_once()
                except Exception:
                    logger.exception("derivation worker iteration failed")
                await asyncio.sleep(2.0)

        asyncio.create_task(_worker_loop())
        logger.info("whatsapp channel mounted; outbox and recovery worker started")
    except Exception:
        logger.exception("failed to compose whatsapp services")
    yield

    if _outbox is not None:
        try:
            await _outbox.stop()
        except Exception:
            logger.exception("error stopping outbox worker")


app.router.lifespan_context = lifespan


def services_storage(cipher: Any | None = None) -> Storage:
    """Build the production storage backend."""
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        import psycopg
        from psycopg.rows import dict_row

        from waxprep.storage.postgres import PostgresStorage

        if cipher is None:
            cipher = build_cipher()
        conn = psycopg.connect(database_url, row_factory=dict_row)  # type: ignore[arg-type]
        return PostgresStorage(conn, cipher)
    from waxprep.storage.memory import InMemoryStorage

    return InMemoryStorage()


def build_cipher():
    """Load AES-GCM field cipher from environment (or deterministic test key)."""

    from waxprep.security.cipher import AesGcmFieldCipher, KeyRing, load_cipher_from_env

    try:
        return load_cipher_from_env()
    except RuntimeError:
        # Local/dev without keys: fixed test key so the process can still boot.
        # Production must set WAXPREP_MASTER_KEYS.
        raw = b"0" * 32
        return AesGcmFieldCipher(KeyRing(keys={1: raw}, active_key_id=1))


def build_context_model(settings: Any = None):
    """Prefer gateway-backed CI model when Upstage is configured; else mock."""
    from waxprep.context.mock_model import MockContextModel

    try:
        if settings is not None and getattr(settings, "upstage_api_key", None):
            from waxprep.context.gateway_model import GatewayContextModel

            gateway = build_gateway(settings)
            return GatewayContextModel(gateway)
    except Exception:
        logger.exception("falling back to MockContextModel for CI")
    return MockContextModel()


def build_gateway(settings: Any):
    """Register Upstage provider + logical teacher/context models."""
    from waxprep.model_gateway.gateway import ModelGateway
    from waxprep.model_gateway.models import ModelCapability, ModelRole
    from waxprep.model_gateway.providers.upstage import UpstageResponsesProvider
    from waxprep.model_gateway.registry import ModelConfig, ModelRegistry

    registry = ModelRegistry()
    api_key = getattr(settings, "upstage_api_key", None) or os.environ.get(
        "WAXPREP_UPSTAGE_API_KEY", ""
    )
    base_url = (
        getattr(settings, "upstage_base_url", None) or "https://api.upstage.ai/v1"
    )
    provider = UpstageResponsesProvider(api_key=api_key or "missing", base_url=base_url)
    registry.register_provider(provider)
    endpoint = getattr(settings, "upstage_model", None) or "solar-mini4"
    caps = provider.capabilities(endpoint)
    for name, role in (
        ("teacher", ModelRole.TEACHER),
        ("context", ModelRole.CONTEXT),
    ):
        registry.register_model(
            ModelConfig(
                name=name,
                provider=provider.name,
                model=endpoint,
                role=role,
                capabilities=caps
                if caps
                else frozenset(
                    {
                        ModelCapability.TEXT_INPUT,
                        ModelCapability.STREAMING,
                        ModelCapability.TOOL_CALLING,
                    }
                ),
            )
        )
    return ModelGateway(registry)


def build_whatsapp_client(settings: Any):
    from waxprep.channel.whatsapp.client import WhatsAppClient

    return WhatsAppClient(
        getattr(settings, "whatsapp_token", None) or "",
        getattr(settings, "whatsapp_phone_number_id", None) or "",
        api_version=getattr(settings, "whatsapp_api_version", None) or "v21.0",
    )
