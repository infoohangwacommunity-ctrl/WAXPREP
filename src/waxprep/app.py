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


def _apply_pending_migrations() -> None:
    """Run schema migrations against DATABASE_URL (idempotent)."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL unset; skipping migrations")
        return
    try:
        import psycopg

        from waxprep.migrations import apply_migrations

        with psycopg.connect(database_url) as conn:
            applied = apply_migrations(conn)
            if applied:
                logger.info("applied migrations: %s", ", ".join(applied))
            else:
                logger.info("migrations up to date")
    except Exception:
        logger.exception("migration apply failed")


app = FastAPI(title="Wax Prep", version=__version__)

_services: Storage | None = None
_turn_service: Any = None
_history: Any = None
_clock: Any = None
_outboxes: list[Any] = []


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

        # Clear a poisoned aborted transaction so health can recover.
        try:
            conn = getattr(_services, "_conn", None)
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        _services.get_student(new_wax_id())
    except Exception as exc:  # noqa: BLE001 — health must never crash
        logger.exception("health check failed: storage unreachable")
        status["status"] = "degraded"
        status["reason"] = f"storage unreachable: {type(exc).__name__}"
    return status


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Compose services, start workers, and shut down cleanly."""
    global _services, _turn_service, _history, _clock, _outboxes

    settings = load_settings()

    try:
        # Always apply migrations when DATABASE_URL is present, even if no
        # channel is enabled yet — keeps schema in sync with the code.
        _apply_pending_migrations()
    except Exception:
        logger.exception("startup migration phase failed")

    if not settings.whatsapp_enabled and not settings.telegram_enabled:
        # Still wire storage so /health can probe the database.
        try:
            cipher = build_cipher()
            storage = services_storage(cipher)
            _services = storage
        except Exception:
            logger.exception("storage composition without channels failed")
        yield
        return

    try:
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
        active_storage = storage
        _services = active_storage
        _turn_service = services.turns
        _history = services.history
        _clock = clock

        from uuid import uuid4

        from waxprep.domain.identifiers import new_wax_id
        from waxprep.domain.models import ChannelIdentity, Student, StudentStatus

        def provision(channel: str, sender: str):
            identity = active_storage.get_channel_identity(channel, sender)
            if identity is not None:
                return identity.wax_id
            now = clock.now()
            wax = new_wax_id()
            active_storage.create_student(
                Student(
                    wax_id=wax,
                    status=StudentStatus.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                active_storage.create_channel_identity(
                    ChannelIdentity(
                        id=uuid4(),
                        wax_id=wax,
                        channel=channel,
                        external_id=sender,
                        created_at=now,
                    )
                )
            except Exception:
                # UniqueViolation aborts the transaction; clear it so lookup works.
                try:
                    active_storage._conn.rollback()  # type: ignore[attr-defined]
                except Exception:
                    pass
                existing = active_storage.get_channel_identity(channel, sender)
                if existing is not None:
                    logger.info(
                        "provision raced; using existing channel identity",
                        extra={
                            "wax_id": str(existing.wax_id),
                            "channel": channel,
                            "sender": sender,
                        },
                    )
                    return existing.wax_id
                raise
            logger.info(
                "provisioned channel student",
                extra={"wax_id": str(wax), "channel": channel, "sender": sender},
            )
            return wax

        if settings.whatsapp_enabled:
            from waxprep.channel.whatsapp.routes import build_whatsapp_router

            client = build_whatsapp_client(settings)

            async def accept_wa(msg: NormalizedInbound) -> InboundResult | None:
                handler = WhatsAppInboundHandler(
                    storage=active_storage,
                    history=services.history,
                    turn_service=services.turns,
                    clock=clock,
                    config=WhatsAppInboundConfig(channel="whatsapp"),
                    whatsapp_client=client,
                )
                return await handler.handle_message(msg)

            app.include_router(
                build_whatsapp_router(
                    verify_token=settings.whatsapp_verify_token or "",
                    app_secret=settings.whatsapp_app_secret or "",
                    resolve_sender=lambda s: provision("whatsapp", s),
                    accept_inbound=accept_wa,
                )
            )
            wa_outbox = WhatsAppOutbox(
                storage=active_storage,
                client=client,
                clock=clock,
                config=OutboxConfig(),
            )
            _outboxes.append(wa_outbox)
            asyncio.create_task(wa_outbox.start())
            logger.info("whatsapp channel mounted")

        if settings.telegram_enabled:
            from waxprep.channel.telegram.client import TelegramClient
            from waxprep.channel.telegram.outbox import (
                TelegramOutbox,
                TelegramOutboxConfig,
            )
            from waxprep.channel.telegram.routes import build_telegram_router

            tg_client = TelegramClient(settings.telegram_bot_token or "")

            async def accept_tg(msg: NormalizedInbound) -> InboundResult | None:
                handler = WhatsAppInboundHandler(
                    storage=active_storage,
                    history=services.history,
                    turn_service=services.turns,
                    clock=clock,
                    config=WhatsAppInboundConfig(channel="telegram"),
                    # TelegramClient duck-types as WhatsAppClient: both
                    # expose ``send_text`` and ``send_typing``. The cast
                    # keeps the type checker happy without forcing a
                    # shared base class or a Protocol refactor.
                    whatsapp_client=tg_client,  # type: ignore[arg-type]
                )
                return await handler.handle_message(msg)

            app.include_router(
                build_telegram_router(
                    secret_token=settings.telegram_webhook_secret,
                    resolve_sender=lambda s: provision("telegram", s),
                    accept_inbound=accept_tg,
                )
            )
            tg_outbox = TelegramOutbox(
                storage=active_storage,
                client=tg_client,
                clock=clock,
                config=TelegramOutboxConfig(),
            )
            _outboxes.append(tg_outbox)
            asyncio.create_task(tg_outbox.start())

            # Register webhook with Telegram if public URL is known
            base = (settings.public_base_url or "").rstrip("/")
            if base:
                webhook_url = f"{base}/channels/telegram/webhook"
                try:
                    info = await tg_client.set_webhook(
                        webhook_url,
                        secret_token=settings.telegram_webhook_secret,
                    )
                    logger.info("telegram webhook set", extra={"result": info})
                except Exception:
                    logger.exception("failed to set telegram webhook")

            logger.info("telegram channel mounted")

        async def _worker_loop() -> None:
            while True:
                try:
                    await services.worker.run_once()
                except Exception:
                    logger.exception("derivation worker iteration failed")
                await asyncio.sleep(settings.worker_interval_seconds)

        if settings.worker_enabled:
            asyncio.create_task(_worker_loop())
            logger.info(
                "merged background worker started (interval=%ss)",
                settings.worker_interval_seconds,
            )
        logger.info("waxprep channels ready")
    except Exception:
        logger.exception("failed to compose channel services")
    yield

    for box in list(_outboxes):
        try:
            await box.stop()
        except Exception:
            logger.exception("error stopping outbox")


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
            effort = getattr(settings, "reasoning_effort", None) or "medium"
            return GatewayContextModel(gateway, reasoning_effort=effort)
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
    endpoint = getattr(settings, "upstage_model", None) or "solar-pro4"
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
