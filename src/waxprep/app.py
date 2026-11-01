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
from waxprep.channel.message_queue import (
    MessageQueue,
    ModelSemanticGrouper,
    QueueConfig,
)
from waxprep.channel.whatsapp.handler import (
    InboundResult,
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.channel.whatsapp.outbox import OutboxConfig, WhatsAppOutbox
from waxprep.clock import SystemClock
from waxprep.domain.identifiers import WaxId
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.media.processors.gemini_stt import build_speech_to_text
from waxprep.orchestration.latency import (
    TurnLatencyRecorder,
    latency_sample_count,
    median_latency_seconds,
)
from waxprep.settings import Settings, load_settings
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

# Graceful-shutdown state (Fix 1 + Fix 2).
#
# ``_draining`` flips to True at the START of the lifespan teardown.
# Webhook ``accept_inbound`` callbacks check it via ``is_draining()`` and
# reject new arrivals with None (the channel adapter returns 503 to the
# provider, which retries against the next replica). This stops the
# redeploy-aborts-mid-turn-transaction vector flagged in
# migration 015's postmortem: the platform sends SIGTERM, uvicorn stops
# accepting new connections, AND the app rejects any in-flight webhook
# already accepted but not yet queued — so no NEW turn can start while
# the drain waits for turns already in flight to finish.
#
# ``_worker_task`` is the derivation worker's asyncio task. The previous
# finding: it was fire-and-forget (``asyncio.create_task`` with no
# reference held), so the lifespan teardown could not cancel + await it.
# A mid-model-call kill is recoverable (job leases expire) but not clean;
# we now cancel and await it explicitly inside the drain timeout.
#
# ``_storage_handles`` tracks every Storage we open (webhook, worker,
# outbox) so the drain can close their underlying psycopg connections.
# Without this, uvicorn's process exit leaks the connections and the
# next cold start races the old connection's COMMIT/ROLLBACK state.
_draining: bool = False
_worker_task: asyncio.Task[None] | None = None
_storage_handles: list[Any] = []


def is_draining() -> bool:
    """True once the lifespan teardown has started (SIGTERM drain in progress).

    Webhook ``accept_inbound`` callbacks read this to reject new arrivals
    so a redeploy cannot start a turn it will not finish.
    """
    return _draining


def _set_draining(value: bool) -> None:
    global _draining
    _draining = value


async def _drain_shutdown(
    *,
    queue: MessageQueue | None = None,
    worker_task: asyncio.Task[None] | None = None,
    outboxes: list[Any] | None = None,
    storage_handles: list[Any] | None = None,
    drain_timeout: float = 25.0,
    queue_stop_timeout: float = 5.0,
    worker_cancel_timeout: float = 5.0,
) -> None:
    """SIGTERM graceful drain — Fix 1 + Fix 2.

    Sequence (the Founder's brief: "stops accepting new webhooks, waits
    for in-flight turns to complete with a timeout, cleanly closes DB
    connections"):

    1. Flip ``_draining`` so webhook callbacks reject new arrivals
       (the channel adapter returns 503; the provider retries the next
       replica). Uvicorn has already stopped accepting new connections
       at the socket level; this is the application-level counterpart.
    2. Wait for the MessageQueue's active turn count to reach zero,
       bounded by ``drain_timeout``. The queue's ``get_active_count``
       reflects turns mid-execution in ``_run_batch`` (the turn runner
       the queue calls — exactly the "in-flight turn" the brief names).
    3. Cancel + await the derivation worker task (Fix 2). The previous
       fire-and-forget ``create_task`` left this untracked; we now hold
       the reference and cancel it inside the drain window so a
       mid-model-call kill is at least a clean CancelledError.
    4. Stop the message queue (cancels any straggler worker tasks).
    5. Stop each outbox (flips ``_running = False``).
    6. Close every psycopg connection we opened. Without this the
       process exit leaks them and the next cold start races the old
       connection's transaction state.

    Every step is best-effort: a drain failure must never mask the
    original shutdown signal. The 25s default leaves 5s of headroom
    under uvicorn's ``--timeout-graceful-shutdown 30``.
    """
    _set_draining(True)
    logger.info("drain: stopped accepting new webhooks")

    # 2. Wait for in-flight turns to complete (with timeout).
    if queue is not None:
        try:
            deadline = asyncio.get_event_loop().time() + drain_timeout
            while queue.get_active_count() > 0:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    logger.warning(
                        "drain: %d in-flight turns still active after %.1fs; "
                        "cancelling",
                        queue.get_active_count(),
                        drain_timeout,
                    )
                    break
                await asyncio.sleep(min(0.5, remaining))
        except Exception:
            logger.exception("drain: error waiting for in-flight turns")

        # 4. Stop the queue (cancels straggler workers).
        try:
            await asyncio.wait_for(queue.stop(), timeout=queue_stop_timeout)
        except TimeoutError:
            logger.warning(
                "drain: message queue stop timed out after %.1fs", queue_stop_timeout
            )
        except Exception:
            logger.exception("drain: error stopping message queue")

    # 3. Cancel + await the derivation worker task (Fix 2).
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await asyncio.wait_for(worker_task, timeout=worker_cancel_timeout)
        except TimeoutError:
            logger.warning(
                "drain: worker task did not cancel within %.1fs", worker_cancel_timeout
            )
        except asyncio.CancelledError:
            # Expected: the task observed the cancellation.
            pass
        except Exception:
            logger.exception("drain: error awaiting worker task")

    # 5. Stop outboxes.
    for box in list(outboxes or []):
        try:
            await box.stop()
        except Exception:
            logger.exception("drain: error stopping outbox")

    # 6. Close DB connections.
    for handle in list(storage_handles or []):
        conn = getattr(handle, "_conn", None)
        if conn is None:
            continue
        closer = getattr(conn, "close", None)
        if not callable(closer):
            continue
        try:
            closer()
        except Exception:
            logger.exception("drain: error closing storage connection")


@app.get("/health", tags=["system"])
def health() -> dict[str, Any]:
    """Report process liveness and median turn latency.

    Composition is optional until lifespan runs. The
    ``latency_median_ms`` field appears only once at least one turn has
    completed (cold boot reports honestly instead of synthesising a
    zero). This is a monitoring signal — NOT an admin dashboard — so it
    surfaces only the median of the last 100 turns plus the sample
    count, nothing per-student.
    """
    status: dict[str, Any] = {
        "status": "ok",
        "service": "waxprep",
        "version": __version__,
    }
    if _services is None:
        status["composition"] = "pending"
    else:
        try:
            from waxprep.domain.identifiers import new_wax_id

            # PostgresStorage heals a poisoned connection itself; a plain
            # read is enough to prove the database is reachable.
            _services.get_student(new_wax_id())
        except Exception as exc:  # noqa: BLE001 — health must never crash
            logger.exception("health check failed: storage unreachable")
            status["status"] = "degraded"
            status["reason"] = f"storage unreachable: {type(exc).__name__}"
    if is_draining():
        status["status"] = "draining"
        status["reason"] = "graceful shutdown in progress"
    median = median_latency_seconds()
    if median is not None:
        status["latency_median_ms"] = round(median * 1000.0, 1)
        status["latency_samples"] = latency_sample_count()
    return status


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Compose services, start workers, and shut down cleanly."""
    global _services, _turn_service, _history, _clock, _outboxes, message_queue
    global _worker_task, _storage_handles

    # Reset drain state in case the module is reloaded between tests.
    _set_draining(False)
    _worker_task = None
    _storage_handles = []

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
            _storage_handles.append(storage)
        except Exception:
            logger.exception("storage composition without channels failed")
        try:
            yield
        finally:
            await _drain_shutdown(
                queue=message_queue,
                worker_task=_worker_task,
                outboxes=_outboxes,
                storage_handles=_storage_handles,
            )
            _storage_handles.clear()
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
        # Track every connection we opened so the drain can close them.
        _storage_handles.extend([storage, worker_storage, outbox_storage])
        clock = SystemClock()
        # One gateway instance for the whole composition; the judge
        # runs over the same provider under its own logical role.
        from waxprep.judge.gateway_model import GatewayJudgeModel

        gateway = build_gateway(settings)
        context_model = build_context_model(settings)
        judge_model = GatewayJudgeModel(gateway)
        # Meaning search: the provider is optional; when configured it
        # turns on semantic retrieval and background embedding jobs.
        embedder = build_embedder(settings)
        services = build_services(
            storage=storage,
            cipher=cipher,
            clock=clock,
            context_model=context_model,
            teacher=None,
            gateway=gateway,
            judge_model=judge_model,
            embedder=embedder,
            # Notes of time: per-stage latency events flow through each
            # composition's own connection (webhook here, worker below).
            latency_recorder=TurnLatencyRecorder(storage, clock),
        )
        worker_services = build_services(
            storage=worker_storage,
            cipher=cipher,
            clock=clock,
            context_model=context_model,
            teacher=None,
            gateway=gateway,
            judge_model=judge_model,
            embedder=embedder,
            latency_recorder=TurnLatencyRecorder(worker_storage, clock),
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
        # resettable quiet period plus a hard collection deadline into
        # single tutor turns. Opt-in small-model semantic grouping
        # classifies arrivals (continuation vs new request); the time
        # window stays the default fallback. One turn per student at a
        # time, strict FIFO order. Handlers are singletons so
        # per-message idempotency locks actually serialize webhook
        # deliveries process-wide.
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

        queue_config = build_queue_config()
        semantic_grouper: ModelSemanticGrouper | None = None
        if queue_config.semantic_grouping_enabled:
            # Cheap classification model under its own logical role so
            # operators can point it at a small endpoint. Any failure
            # inside falls back to the time window.
            try:
                semantic_grouper = ModelSemanticGrouper(gateway, model_name="grouping")
            except Exception:
                logger.exception("semantic grouper unavailable; time window only")
        message_queue = MessageQueue(
            clock,
            queue_config,
            turn_runner=_queue_turn_runner,
            semantic_grouper=semantic_grouper,
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
                speech_to_text=build_speech_to_text(settings),
            )
            queue_handlers["whatsapp"] = wa_handler

            async def accept_wa(msg: NormalizedInbound) -> InboundResult | None:
                # Drain guard (Fix 1): once SIGTERM lands, no new turn
                # may start. Returning None lets the channel adapter
                # respond 503 so Meta/Telegram retries the next replica
                # instead of starting a turn this process will not finish.
                if is_draining():
                    logger.info(
                        "webhook rejected: drain in progress",
                        extra={"channel": "whatsapp"},
                    )
                    return None
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
                speech_to_text=build_speech_to_text(settings),
            )
            queue_handlers["telegram"] = tg_handler

            async def accept_tg(
                msg: NormalizedInbound,
            ) -> TelegramInboundResult | None:
                # Drain guard (Fix 1): mirror the WhatsApp path so a
                # SIGTERM during a Telegram burst cannot start a new
                # turn this process will not finish.
                if is_draining():
                    logger.info(
                        "webhook rejected: drain in progress",
                        extra={"channel": "telegram"},
                    )
                    return None
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
            while not is_draining():
                try:
                    await worker_services.worker.run_once()
                except Exception:
                    logger.exception("derivation worker iteration failed")
                await asyncio.sleep(2.0)

        # Fix 2: hold the task reference so the drain can cancel +
        # await it inside the graceful-shutdown window. The previous
        # fire-and-forget ``asyncio.create_task`` left this untracked;
        # a mid-model-call kill was recoverable (job leases expire)
        # but not clean, and the unawaited task also produced an
        # "asyncio task was destroyed but it is pending" warning on
        # every uvicorn shutdown.
        _worker_task = asyncio.create_task(_worker_loop())
        logger.info("waxprep channels ready")
    except Exception:
        logger.exception("failed to compose channel services")
    try:
        yield
    finally:
        # SIGTERM drain (Fix 1 + Fix 2). Runs whether composition
        # succeeded or not so partial state still tears down cleanly.
        await _drain_shutdown(
            queue=message_queue,
            worker_task=_worker_task,
            outboxes=_outboxes,
            storage_handles=_storage_handles,
        )
        _worker_task = None
        _storage_handles.clear()


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
    """Load AES-GCM field cipher from environment (fail-closed).

    Founder's vision §8 — "capability truth": never silently fabricate
    a key so the process can boot. ``WAXPREP_MASTER_KEYS`` MUST be set
    in production. A test marker (``WAXPREP_TEST_ALLOW_INSECURE_KEYS=1``)
    is the ONLY escape hatch — it lets the unit-test suite and local
    dev boot without real keys, and it is intentionally loud so an
    operator reading logs can never miss that an insecure cipher is
    in use. Render has the keys set, so the fail-closed path is the
    production path.
    """

    from waxprep.security.cipher import load_cipher_from_env

    try:
        return load_cipher_from_env()
    except RuntimeError as exc:
        # Fail-closed unless the explicit test marker is present. The
        # previous behaviour silently fell back to ``b"0" * 32`` which
        # meant production could boot without an operator ever noticing
        # the missing secret — exactly the silent MockContextModel-style
        # failure the founder's capability-truth rule forbids.
        marker = os.environ.get("WAXPREP_TEST_ALLOW_INSECURE_KEYS", "").lower()
        if marker not in {"1", "true", "yes"}:
            raise RuntimeError(
                "WAXPREP_MASTER_KEYS is not set. Production must configure "
                "real encryption keys. To permit an insecure test key "
                "(local dev or unit tests only), set "
                "WAXPREP_TEST_ALLOW_INSECURE_KEYS=1 explicitly."
            ) from exc
        logger.warning(
            "WAXPREP_TEST_ALLOW_INSECURE_KEYS=1 — using deterministic "
            "test cipher. This MUST NEVER happen in production."
        )
        from waxprep.security.cipher import AesGcmFieldCipher, KeyRing

        raw = b"0" * 32
        return AesGcmFieldCipher(KeyRing(keys={1: raw}, active_key_id=1))


def build_context_model(settings: Any = None):
    """Build the production Context Intelligence model (fail-closed).

    Founder's vision §8 — "capability truth": never claim to have run
    a Context Intelligence investigation when the model was never
    wired. ``MockContextModel`` is the test double — it returns canned
    decisions and would silently fabricate context in production. We
    refuse to fall back to it unless the explicit test marker
    ``WAXPREP_TEST_ALLOW_MOCK_CONTEXT_MODEL=1`` is set, so a missing
    provider configuration fails loudly at startup instead of producing
    fake context the Teacher cannot distinguish from real evidence.

    When a real Upstage API key is configured, the gateway-backed
    ``GatewayContextModel`` is wired up as before.
    """
    from waxprep.context.mock_model import MockContextModel

    if settings is not None and getattr(settings, "upstage_api_key", None):
        try:
            from waxprep.context.gateway_model import GatewayContextModel

            gateway = build_gateway(settings)
            effort = getattr(settings, "reasoning_effort", None) or "medium"
            return GatewayContextModel(gateway, reasoning_effort=effort)
        except Exception:
            # The gateway failed to build. Do NOT fall back to the
            # mock silently — that is exactly the capability-truth
            # violation the founder named. Surface the failure.
            logger.exception(
                "gateway-backed context model failed to build; refusing "
                "to silently fall back to MockContextModel in production"
            )
            raise
    # No real provider configured. Fail-closed unless the test marker
    # is present. Local dev and the unit-test suite set the marker; the
    # production boot path never does.
    marker = os.environ.get("WAXPREP_TEST_ALLOW_MOCK_CONTEXT_MODEL", "").lower()
    if marker in {"1", "true", "yes"}:
        logger.warning(
            "WAXPREP_TEST_ALLOW_MOCK_CONTEXT_MODEL=1 — using MockContextModel. "
            "CI will return canned decisions; this MUST NEVER happen in production."
        )
        return MockContextModel()
    raise RuntimeError(
        "No Context Intelligence provider is configured (WAXPREP_UPSTAGE_API_KEY "
        "is unset). Production MUST configure a real provider. To permit the "
        "MockContextModel test double (local dev or unit tests only), set "
        "WAXPREP_TEST_ALLOW_MOCK_CONTEXT_MODEL=1 explicitly."
    )


def build_gateway(settings: Any):
    """Register Upstage provider + logical teacher/context models.

    Latency fix (founder's brief): one shared ``httpx.AsyncClient`` is
    constructed at wiring time and passed to every provider that talks
    HTTP. Each provider used to call ``httpx.AsyncClient()`` per
    request — paying the TCP-setup cost every time. The shared client
    keeps connections alive across requests and across models (teacher,
    context, judge) so a typical turn's three model calls reuse the
    same warm connection pool. The provider NEVER closes the shared
    client after a call (only its own per-request client, when one was
    created — which is now the rare fallback path).
    """
    import httpx

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
    # ONE shared client across every Upstage call (teacher, context,
    # judge, grouping). Connection pool stays warm for the lifetime of
    # the gateway; the provider never closes it.
    shared_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    provider = UpstageResponsesProvider(
        api_key=api_key or "missing",
        base_url=base_url,
        client=shared_client,
    )
    registry.register_provider(provider)
    endpoint = getattr(settings, "upstage_model", None) or "solar-pro4"
    caps = provider.capabilities(endpoint)
    for name, role in (
        ("teacher", ModelRole.TEACHER),
        ("context", ModelRole.CONTEXT),
        ("judge", ModelRole.JUDGE),
        ("grouping", ModelRole.GROUPING),
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


def build_embedder(settings: Settings) -> EmbeddingProvider | None:
    """Optional OpenAI-compatible embedding provider for meaning search.

    The semantic retrieval capability already exists (``find_by_meaning``);
    wiring a provider here turns it on in production, including the
    background embedding jobs the indexer schedules. Unset means
    lexical-only retrieval, which the meaning-search tool reports
    honestly instead of pretending semantic search succeeded.

    Latency fix (founder's brief): when an embedding provider is wired,
    it receives the SAME shared ``httpx.AsyncClient`` used by the
    model gateway so embedding calls reuse the warm connection pool.
    """
    if settings.embedding_api_key is None or settings.embedding_model is None:
        return None
    import httpx

    from waxprep.embeddings.http import HttpEmbeddingProvider

    shared_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    return HttpEmbeddingProvider(
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.media_limits.request_timeout_seconds,
        client=shared_client,
    )


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
    WAXPREP_QUEUE_WINDOW    — coalescing window seconds (default 0.5; founder's
    latency target drops queue-induced wait to ~0.5s for fast-path turns)
    WAXPREP_QUEUE_MAX_BATCH — max messages coalesced per turn (default 5)
    WAXPREP_QUEUE_MAX_COLLECTION — hard collection deadline seconds from
    the first pending message (default 3.0; balances batching vs latency)
    WAXPREP_QUEUE_SEMANTIC — "0"/"1" small-model semantic grouping
    (default on; falls back to the time window whenever the model fails)
    WAXPREP_QUEUE_GROUPING_TIMEOUT — semantic grouping call budget
    seconds (default 1.5)
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
        coalesce_window_seconds=_positive_float("WAXPREP_QUEUE_WINDOW", 0.5),
        max_batch_messages=_positive_int("WAXPREP_QUEUE_MAX_BATCH", 5),
        max_concurrent_per_student=1,
        max_collection_seconds=_positive_float("WAXPREP_QUEUE_MAX_COLLECTION", 3.0),
        semantic_grouping_enabled=_flag("WAXPREP_QUEUE_SEMANTIC", True),
        grouping_timeout_seconds=_positive_float("WAXPREP_QUEUE_GROUPING_TIMEOUT", 1.5),
    )
