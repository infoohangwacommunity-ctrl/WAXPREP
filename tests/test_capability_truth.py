"""Founder's vision §8 — capability truth.

``build_cipher`` and ``build_context_model`` must NOT silently fall
back to the test double when production secrets are missing. The
previous behaviour masked a missing ``WAXPREP_MASTER_KEYS`` (the
process booted with ``b"0" * 32``) and a missing Upstage API key
(CI silently used ``MockContextModel``). Both are exactly the
"capability truth" violation the Founder named: never claim to have
encrypted something or investigated something when the capability
was not actually wired.

This module pins the new contract:

1. ``build_cipher()`` raises ``RuntimeError`` when
   ``WAXPREP_MASTER_KEYS`` is unset, UNLESS the explicit test
   marker ``WAXPREP_TEST_ALLOW_INSECURE_KEYS=1`` is set.
2. ``build_context_model(settings)`` raises ``RuntimeError`` when no
   real provider is configured, UNLESS the explicit test marker
   ``WAXPREP_TEST_ALLOW_MOCK_CONTEXT_MODEL=1`` is set.

The test markers exist for local dev and the unit-test suite; the
production boot path never sets them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every WAXPREP_* secret + marker for the duration of one test."""
    for key in (
        "WAXPREP_MASTER_KEYS",
        "WAXPREP_ACTIVE_KEY_ID",
        "WAXPREP_UPSTAGE_API_KEY",
        "WAXPREP_TEST_ALLOW_INSECURE_KEYS",
        "WAXPREP_TEST_ALLOW_MOCK_CONTEXT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# --- build_cipher ----------------------------------------------------------


def test_build_cipher_fails_closed_without_master_keys(
    clean_env: None,  # noqa: ARG001 — fixture cleans env
) -> None:
    """Production must configure ``WAXPREP_MASTER_KEYS``; missing it must
    raise, not silently fabricate a key."""
    from waxprep.app import build_cipher

    with pytest.raises(RuntimeError, match="WAXPREP_MASTER_KEYS"):
        build_cipher()


def test_build_cipher_marker_allows_test_key(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,  # noqa: ARG001 — fixture cleans env
) -> None:
    """The test marker opts INTO the insecure test key — explicit, loud,
    and never the production default."""
    monkeypatch.setenv("WAXPREP_TEST_ALLOW_INSECURE_KEYS", "1")
    from waxprep.app import build_cipher

    cipher = build_cipher()
    # Round-trip proves the cipher works; the warning is logged but not
    # asserted here — the contract is "marker present -> test cipher
    # usable", not "marker present -> silent". The cipher is bound to
    # the wax_id, so the same WaxId must be used for encrypt + decrypt.
    wax_id = _wax()
    blob = cipher.encrypt_text(wax_id, "purpose", "hello")
    assert cipher.decrypt_text(wax_id, "purpose", blob) == "hello"


def test_build_cipher_uses_real_keys_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,  # noqa: ARG001 — fixture cleans env
) -> None:
    """When ``WAXPREP_MASTER_KEYS`` is set, the real cipher loads and the
    test marker is irrelevant."""
    monkeypatch.setenv(
        "WAXPREP_MASTER_KEYS",
        "1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    monkeypatch.setenv("WAXPREP_ACTIVE_KEY_ID", "1")
    # Even if the marker is unset, real keys win.
    from waxprep.app import build_cipher

    cipher = build_cipher()
    wax_id = _wax()
    blob = cipher.encrypt_text(wax_id, "purpose", "hello")
    assert cipher.decrypt_text(wax_id, "purpose", blob) == "hello"


# --- build_context_model ---------------------------------------------------


class _NoUpstageSettings:
    """Minimal settings stub: no Upstage key configured."""

    upstage_api_key: str | None = None
    reasoning_effort: str | None = None


class _UpstageSettings:
    """Settings stub with a (fake) Upstage key — but no real network."""

    upstage_api_key: str | None = "test-key-not-real"
    reasoning_effort: str | None = "medium"
    upstage_base_url: str | None = "https://api.upstage.invalid/v1"
    upstage_model: str | None = "solar-pro4"


def test_build_context_model_fails_closed_without_provider(
    clean_env: None,  # noqa: ARG001 — fixture cleans env
) -> None:
    """No Upstage key + no marker -> RuntimeError, not silent MockContextModel."""
    from waxprep.app import build_context_model

    with pytest.raises(RuntimeError, match="No Context Intelligence provider"):
        build_context_model(_NoUpstageSettings())


def test_build_context_model_marker_allows_mock(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,  # noqa: ARG001 — fixture cleans env
) -> None:
    """The test marker is the ONLY opt-in to MockContextModel."""
    monkeypatch.setenv("WAXPREP_TEST_ALLOW_MOCK_CONTEXT_MODEL", "1")
    from waxprep.app import build_context_model
    from waxprep.context.mock_model import MockContextModel

    model = build_context_model(_NoUpstageSettings())
    assert isinstance(model, MockContextModel)


def test_build_context_model_does_not_silently_swallow_gateway_errors(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,  # noqa: ARG001 — fixture cleans env
) -> None:
    """When the gateway-backed model fails to build, the failure must
    propagate — never silently fall back to MockContextModel."""
    # The fake settings point at a non-existent base_url; the real
    # build_gateway path constructs an httpx client (no network call
    # at construction time) so we need to break the import to force the
    # except branch. We do that by patching GatewayContextModel.
    import waxprep.context.gateway_model as gw_mod
    from waxprep.app import build_context_model

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated gateway construction failure")

    monkeypatch.setattr(gw_mod, "GatewayContextModel", _Boom)
    with pytest.raises(RuntimeError, match="simulated gateway construction"):
        build_context_model(_UpstageSettings())


# --- helpers ---------------------------------------------------------------


def _wax() -> Any:
    from waxprep.domain.identifiers import new_wax_id

    return new_wax_id()


__all__ = [
    "test_build_cipher_fails_closed_without_master_keys",
    "test_build_cipher_marker_allows_test_key",
    "test_build_cipher_uses_real_keys_when_configured",
    "test_build_context_model_fails_closed_without_provider",
    "test_build_context_model_marker_allows_mock",
    "test_build_context_model_does_not_silently_swallow_gateway_errors",
]
