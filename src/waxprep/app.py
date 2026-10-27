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
from waxprep.channel.message_queue import MessageQueue, QueueConfig
from waxprep.channel.whatsapp.handler import (
    InboundResult,
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.channel.whatsapp.outbox import OutboxConfig, WhatsAppOutbox
from waxprep.clock import SystemClock
from waxprep.domain.identifiers import WaxId
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
message_queue: MessageQueue | None = None
_memory_storage: Storage | None = None


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

        # PostgresStorage heals a poisoned connection itself; a plain
        # read is enough to prove the database is reachable.
        _services.get_student(new_wax_id())
    except Exception as exc:  # noqa: BLE001 — health must never crash
        logger.exception("health check failed: storage unreachable")
        status["status"] = "degraded"
        status["reason"] = f"storage unreachable: {type(exc).__name__}"
    return status


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Compose services, start workers, and shut down cleanly."""
    global _services, _turn_service, _history, _clock, _outboxes, message_queue

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
        # Three separate connections: webhook, derivation worker, and
        # outbox. Sharing one connection let a failed worker statement
        # poison the transaction state for every webhook (live
        # incident: each WhatsApp message 500'd, Meta retried, and
        # every retry created another orphan student).
        storage = services_storage(cipher)
        worker_storage = services_storage(cipher)
        outbox_storage = services_storage(cipher)
        clock = SystemClock()
        # One gateway instance for the whole composition; the judge
        # runs over the same provider under its own logical role.
        from waxprep.judge.gateway_model import GatewayJudgeModel

        gateway = build_gateway(settings)
        context_model = build_context_model(settings)
        judge_model = GatewayJudgeModel(gateway)
        services = build_services(
            storage=storage,
            cipher=cipher,
            clock=clock,
            context_model=context_model,
            teacher=None,
            gateway=gateway,
            judge_model=judge_model,
        )
        worker_services = build_services(
            storage=worker_storage,
            cipher=cipher,
            clock=clock,
            context_model=context_model,
            teacher=None,
            gateway=gateway,
            judge_model=judge_model,
        )
        active_storage = storage
        _services = active_storage
        _turn_service = services.turns
        _history = services.history
        _clock = clock

        # Students left behind by failed provisioning (webhook retries
        # that crashed between the two writes) are pruned at startup.
        _prune_orphan_students(active_storage, clock)

        def provision(channel: str, sender: str) -> WaxId:
            return provision_channel_student(active_storage, clock, channel, sender)

        # Shared per-student queue: rapid message bursts are coalesced by a
        # short time window into single tutor turns (arrival cadence only —
        # never keyword matching). One turn per student at a time, strict
        # FIFO order. Handlers are singletons so per-message idempotency
        # locks actually serialize webhook deliveries process-wide.
        queue_handlers: dict[str, Any] = {}

        async def _queue_turn_runner(wax_id: WaxId, batch: list[Any]) -> None:
            channel = getattr(batch[-1], "channel", None)
            handler = queue_handlers.get(channel) if channel else None
            if handler is None and len(queue_handlers) == 1:
                handler = next(iter(queue_handlers.values()))
            if handler is None:
                logger.warning(
                    "queued batch has no registered handler",
                    extra={"channel": channel, "wax_id": str(wax_id)},
                )
                return
            await handler.run_queued_turn(wax_id, batch)

        message_queue = MessageQueue(
            clock,
            build_queue_config(),
            turn_runner=_queue_turn_runner,
        )

        if settings.whatsapp_enabled:
            from waxprep.channel.whatsapp.routes import build_whatsapp_router

            client = build_whatsapp_client(settings)
            wa_handler = WhatsAppInboundHandler(
                storage=active_storage,
                history=services.history,
                turn_service=services.turns,
                clock=clock,
                config=WhatsAppInboundConfig(channel="whatsapp"),
                whatsapp_client=client,
                message_queue=message_queue,
            )
            queue_handlers["whatsapp"] = wa_handler

            async def accept_wa(msg: NormalizedInbound) -> InboundResult | None:
                return await wa_handler.handle_message(msg)

            app.include_router(
                build_whatsapp_router(
                    verify_token=settings.whatsapp_verify_token or "",
                    app_secret=settings.whatsapp_app_secret or "",
                    resolve_sender=lambda s: provision("whatsapp", s),
                    accept_inbound=accept_wa,
                )
            )
            wa_outbox = WhatsAppOutbox(
                storage=outbox_storage,
                client=client,
                clock=clock,
                config=OutboxConfig(),
            )
            _outboxes.append(wa_outbox)
            asyncio.create_task(wa_outbox.start())
            logger.info("whatsapp channel mounted")

        if settings.telegram_enabled:
            from waxprep.channel.telegram.client import TelegramClient
            from waxprep.channel.telegram.handler import (
                InboundResult as TelegramInboundResult,
            )
            from waxprep.channel.telegram.handler import (
                TelegramInboundConfig,
                TelegramInboundHandler,
            )
            from waxprep.channel.telegram.outbox import (
                TelegramOutbox,
                TelegramOutboxConfig,
            )
            from waxprep.channel.telegram.routes import build_telegram_router

            tg_client = TelegramClient(settings.telegram_bot_token or "")

            # Native Telegram inbound handler: draft streaming through
            # the client's real draft API (verified capability), no more
            # WhatsApp-handler adapter with an ignored type error.
            tg_handler = TelegramInboundHandler(
                storage=active_storage,
                history=services.history,
                turn_service=services.turns,
                clock=clock,
                config=TelegramInboundConfig(),
                telegram_client=tg_client,
                message_queue=message_queue,
            )
            queue_handlers["telegram"] = tg_handler

            async def accept_tg(
                msg: NormalizedInbound,
            ) -> TelegramInboundResult | None:
                return await tg_handler.handle_message(msg)

            app.include_router(
                build_telegram_router(
                    secret_token=settings.telegram_webhook_secret,
                    resolve_sender=lambda s: provision("telegram", s),
                    accept_inbound=accept_tg,
                )
            )
            tg_outbox = TelegramOutbox(
                storage=outbox_storage,
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
                    await worker_services.worker.run_once()
                except Exception:
                    logger.exception("derivation worker iteration failed")
                await asyncio.sleep(2.0)

        asyncio.create_task(_worker_loop())
        logger.info("waxprep channels ready")
    except Exception:
        logger.exception("failed to compose channel services")
    yield

    if message_queue is not None:
        try:
            await message_queue.stop()
        except Exception:
            logger.exception("error stopping message queue")

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

    # Without a database the webhook, worker, and outbox still have to
    # observe the same data, so they share one in-memory store
    # (production gives each subsystem its own real connection).
    global _memory_storage
    if _memory_storage is None:
        _memory_storage = InMemoryStorage()
    return _memory_storage


def provision_channel_student(
    storage: Storage, clock: Any, channel: str, sender: str
) -> WaxId:
    """Idempotently provision the student for a channel sender.

    Safe across provider webhook retries: the student and channel
    identity are created in ONE transaction, and a racing retry that
    finds the pair half-built re-reads the identity instead of
    failing (live incident: retries against a 500 left orphan
    students with no identity).
    """
    identity = storage.get_channel_identity(channel, sender)
    if identity is not None:
        return identity.wax_id
    from uuid import uuid4

    from waxprep.domain.identifiers import new_wax_id
    from waxprep.domain.models import ChannelIdentity, Student, StudentStatus

    now = clock.now()
    wax = new_wax_id()
    try:
        with storage.transaction():
            storage.create_student(
                Student(
                    wax_id=wax,
                    status=StudentStatus.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
            )
            storage.create_channel_identity(
                ChannelIdentity(
                    id=uuid4(),
                    wax_id=wax,
                    channel=channel,
                    external_id=sender,
                    created_at=now,
                )
            )
    except Exception:
        # A concurrent webhook retry may have created the pair while
        # this call was in flight; re-read and reuse it.
        retry_identity = storage.get_channel_identity(channel, sender)
        if retry_identity is not None:
            return retry_identity.wax_id
        # Nothing to reuse: remove the half-created student so retries
        # cannot accumulate orphans, then surface the real error.
        try:
            storage.delete_student(wax)
        except Exception:
            logger.exception(
                "orphan student cleanup failed", extra={"wax_id": str(wax)}
            )
        raise
    logger.info(
        "provisioned channel student",
        extra={"wax_id": str(wax), "channel": channel, "sender": sender},
    )
    return wax


def _prune_orphan_students(storage: Storage, clock: Any) -> None:
    """Best-effort removal of students created by failed provisioning.

    A student older than an hour with no channel identity and no data
    of any kind was never reachable: nothing can ever address it again.
    The live incident left exactly such rows behind.
    """
    from datetime import timedelta

    older_than = clock.now() - timedelta(hours=1)
    try:
        orphans = storage.list_orphan_student_ids(older_than)
    except Exception:
        logger.exception("orphan student scan failed")
        return
    removed = 0
    for wax_id in orphans:
        try:
            if storage.delete_student(wax_id):
                removed += 1
        except Exception:
            logger.exception(
                "orphan student removal failed", extra={"wax_id": str(wax_id)}
            )
    if removed:
        logger.info("pruned orphan students", extra={"count": removed})


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
        ("judge", ModelRole.JUDGE),
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


def build_queue_config() -> QueueConfig:
    """Per-student queue knobs from the environment (all optional).

    WAXPREP_QUEUE_COALESCE  — "0"/"1" time-window coalescing (default on)
    WAXPREP_QUEUE_WINDOW    — coalescing window seconds (default 2.0)
    WAXPREP_QUEUE_MAX_BATCH — max messages coalesced per turn (default 5)
    """

    def _flag(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "off", "no"}

    def _positive_float(name: str, default: float) -> float:
        try:
            value = float(os.environ.get(name, ""))
        except ValueError:
            return default
        return value if value > 0 else default

    def _positive_int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, ""))
        except ValueError:
            return default
        return value if value > 0 else default

    return QueueConfig(
        enforce_fifo=True,
        coalesce_enabled=_flag("WAXPREP_QUEUE_COALESCE", True),
        coalesce_window_seconds=_positive_float("WAXPREP_QUEUE_WINDOW", 2.0),
        max_batch_messages=_positive_int("WAXPREP_QUEUE_MAX_BATCH", 5),
        max_concurrent_per_student=1,
    )
