"""WhatsApp webhook authenticity helpers."""

from __future__ import annotations

import hashlib
import hmac


def verify_webhook_challenge(
    mode: str | None,
    token: str | None,
    challenge: str | None,
    expected_verify_token: str,
) -> str | None:
    """Return challenge string if the subscription verification succeeds."""
    if mode == "subscribe" and token == expected_verify_token and challenge:
        return challenge
    return None


def verify_signature(
    app_secret: str, body: bytes, signature_header: str | None
) -> bool:
    """Validate X-Hub-Signature-256 (sha256=<hex>)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header.removeprefix("sha256=")
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)
