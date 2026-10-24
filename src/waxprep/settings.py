"""Application settings loaded from environment.

Secrets never appear in logs. Missing required channel settings fail
clearly when that channel is enabled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaLimits:
    max_inbound_bytes: int = 15 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_document_pages_sync: int = 100
    max_audio_bytes: int = 10 * 1024 * 1024
    max_audio_seconds: int = 600
    max_per_artifact_attempts: int = 3
    request_timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class Settings:
    upstage_api_key: str | None = None
    upstage_base_url: str = "https://api.upstage.ai/v1"
    upstage_document_model: str = "document-parse"
    upstage_model: str = "solar-pro4"
    reasoning_effort: str = "medium"
    voice_provider: str | None = None  # "gemini" | "grok" | None
    gemini_api_key: str | None = None
    xai_api_key: str | None = None
    whatsapp_enabled: bool = False
    whatsapp_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_api_version: str = "v21.0"
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    public_base_url: str | None = None
    media_limits: MediaLimits = MediaLimits()
    blob_root: str = ".waxprep-blobs"
    # Merged background worker (runs inside the web process — no separate
    # service needed). On Render's free tier, this is the only option.
    worker_enabled: bool = True
    worker_interval_seconds: float = 5.0


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = environ if environ is not None else dict(os.environ)
    limits = MediaLimits(
        max_inbound_bytes=int(
            env.get("WAXPREP_MAX_INBOUND_BYTES", str(15 * 1024 * 1024))
        ),
        max_audio_bytes=int(env.get("WAXPREP_MAX_AUDIO_BYTES", str(10 * 1024 * 1024))),
        max_audio_seconds=int(env.get("WAXPREP_MAX_AUDIO_SECONDS", "600")),
        request_timeout_seconds=float(env.get("WAXPREP_MEDIA_TIMEOUT", "60")),
    )
    wa_enabled = env.get("WAXPREP_WHATSAPP_ENABLED", "").lower() in {"1", "true", "yes"}
    tg_enabled = env.get("WAXPREP_TELEGRAM_ENABLED", "").lower() in {"1", "true", "yes"}
    settings = Settings(
        upstage_api_key=env.get("UPSTAGE_API_KEY")
        or env.get("WAXPREP_UPSTAGE_API_KEY"),
        upstage_base_url=env.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1"),
        upstage_document_model=env.get("UPSTAGE_DOCUMENT_MODEL", "document-parse"),
        upstage_model=env.get("UPSTAGE_MODEL")
        or env.get("WAXPREP_UPSTAGE_MODEL")
        or "solar-pro4",
        reasoning_effort=(
            env.get("WAXPREP_REASONING_EFFORT")
            or env.get("REASONING_EFFORT")
            or "medium"
        ).lower(),
        voice_provider=(env.get("WAXPREP_VOICE_PROVIDER") or None),
        gemini_api_key=env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"),
        xai_api_key=env.get("XAI_API_KEY"),
        whatsapp_enabled=wa_enabled,
        whatsapp_token=env.get("WHATSAPP_TOKEN") or env.get("WAXPREP_WHATSAPP_TOKEN"),
        whatsapp_phone_number_id=env.get("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_app_secret=env.get("WHATSAPP_APP_SECRET"),
        whatsapp_verify_token=env.get("WHATSAPP_VERIFY_TOKEN"),
        whatsapp_api_version=env.get("WHATSAPP_API_VERSION", "v21.0"),
        telegram_enabled=tg_enabled,
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN")
        or env.get("WAXPREP_TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=env.get("TELEGRAM_WEBHOOK_SECRET")
        or env.get("WAXPREP_TELEGRAM_WEBHOOK_SECRET"),
        public_base_url=env.get("WAXPREP_PUBLIC_BASE_URL"),
        media_limits=limits,
        blob_root=env.get("WAXPREP_BLOB_ROOT", ".waxprep-blobs"),
        worker_enabled=env.get("WAXPREP_WORKER_ENABLED", "1") != "0",
        worker_interval_seconds=float(env.get("WAXPREP_WORKER_INTERVAL", "5")),
    )
    if settings.whatsapp_enabled:
        missing = [
            name
            for name, val in (
                ("WHATSAPP_TOKEN", settings.whatsapp_token),
                ("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id),
                ("WHATSAPP_APP_SECRET", settings.whatsapp_app_secret),
                ("WHATSAPP_VERIFY_TOKEN", settings.whatsapp_verify_token),
            )
            if not val
        ]
        if missing:
            raise ValueError(
                "WhatsApp is enabled but required settings are missing: "
                + ", ".join(missing)
            )
    if settings.telegram_enabled and not settings.telegram_bot_token:
        raise ValueError("Telegram is enabled but TELEGRAM_BOT_TOKEN is missing")
    if settings.voice_provider not in (None, "gemini", "grok"):
        raise ValueError(
            f"WAXPREP_VOICE_PROVIDER must be 'gemini', 'grok', or unset; "
            f"got {settings.voice_provider!r}"
        )
    if settings.voice_provider == "gemini" and not settings.gemini_api_key:
        raise ValueError("WAXPREP_VOICE_PROVIDER=gemini requires GEMINI_API_KEY")
    if settings.voice_provider == "grok" and not settings.xai_api_key:
        raise ValueError("WAXPREP_VOICE_PROVIDER=grok requires XAI_API_KEY")
    return settings
