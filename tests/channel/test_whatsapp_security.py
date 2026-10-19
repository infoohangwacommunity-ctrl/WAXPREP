import hashlib
import hmac

from waxprep.channel.whatsapp.security import verify_signature, verify_webhook_challenge


def test_challenge_ok() -> None:
    assert verify_webhook_challenge("subscribe", "tok", "12345", "tok") == "12345"
    assert verify_webhook_challenge("subscribe", "bad", "12345", "tok") is None


def test_signature() -> None:
    secret = "app-secret"
    body = b'{"entry":[]}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, f"sha256={sig}")
    assert not verify_signature(secret, body, "sha256=deadbeef")
    assert not verify_signature(secret, body, None)
