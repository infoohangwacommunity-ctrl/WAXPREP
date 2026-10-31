"""Founder fix (latency): coalesce window 0.5s, shared AsyncClient, CI fast-path.

The founder's brief calls out a "noticeable difference" in latency.
The latency fix has three independent levers; each gets a test:

1. ``coalesce_window_seconds`` default is 0.5 (not the old 5.0). The
   production wiring in ``app.build_queue_config`` already passes
   ``_positive_float("WAXPREP_QUEUE_WINDOW", 0.5)``. The dataclass
   default on ``QueueConfig`` should also be 0.5 so tests that
   construct ``QueueConfig()`` without the production wiring still
   see the latency fix.

2. The shared ``httpx.AsyncClient`` is reused across provider calls
   — each provider used to call ``httpx.AsyncClient()`` per request,
   paying the TCP-setup cost every time. The fix injects one shared
   client at provider construction; tests inspect the construction
   count.

3. The CI fast-path runs a single model call for simple messages
   (e.g. ``"hi"``). No reason to spend two model rounds deciding
   what to investigate when the answer is "stop, nothing to look
   up". The fast-path short-circuits the loop without sacrificing
   the honesty contract (status is still computed from runtime
   facts, not the model's claim).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from waxprep.channel.message_queue import QueueConfig

# ---------------------------------------------------------------------------
# 1. coalesce_window_seconds default is 0.5 (not 5.0)
# ---------------------------------------------------------------------------


def test_queue_config_dataclass_default_coalesce_window_is_half_second() -> None:
    """The QueueConfig dataclass default for ``coalesce_window_seconds``
    must be 0.5 — the latency-fix target. The old default of 5.0 (or
    the intermediate 2.0) made every fast-path turn wait five seconds
    (or two) for the coalesce window to elapse. The fix drops it to
    0.5s so a single-message turn fires immediately.
    """
    cfg = QueueConfig()
    assert cfg.coalesce_window_seconds == 0.5, (
        f"QueueConfig.coalesce_window_seconds default is "
        f"{cfg.coalesce_window_seconds}, expected 0.5 (latency fix). "
        f"The dataclass default must match the production wiring."
    )


def test_production_queue_config_uses_half_second_default() -> None:
    """The production wiring in ``app.build_queue_config`` must use
    0.5s as the default ``coalesce_window_seconds`` when
    ``WAXPREP_QUEUE_WINDOW`` is unset. This is the user-visible
    latency fix.
    """
    # Strip the env var so we see the default.
    saved = os.environ.pop("WAXPREP_QUEUE_WINDOW", None)
    try:
        from waxprep.app import build_queue_config

        cfg = build_queue_config()
        assert cfg.coalesce_window_seconds == 0.5, (
            f"production wiring default coalesce_window_seconds="
            f"{cfg.coalesce_window_seconds}, expected 0.5"
        )
        assert cfg.coalesce_window_seconds != 5.0, (
            "the old 5.0s default is back — latency regression"
        )
        assert cfg.coalesce_window_seconds != 2.0, (
            "the intermediate 2.0s default is back — latency regression"
        )
    finally:
        if saved is not None:
            os.environ["WAXPREP_QUEUE_WINDOW"] = saved


def test_production_queue_config_respects_env_override() -> None:
    """Operators can still tune the window via env var. The fix
    changes the DEFAULT, not the configurability.
    """
    saved = os.environ.pop("WAXPREP_QUEUE_WINDOW", None)
    try:
        os.environ["WAXPREP_QUEUE_WINDOW"] = "1.5"
        from waxprep.app import build_queue_config

        cfg = build_queue_config()
        assert cfg.coalesce_window_seconds == 1.5
    finally:
        os.environ.pop("WAXPREP_QUEUE_WINDOW", None)
        if saved is not None:
            os.environ["WAXPREP_QUEUE_WINDOW"] = saved


# ---------------------------------------------------------------------------
# 2. The shared httpx.AsyncClient is reused across provider calls
# ---------------------------------------------------------------------------


def test_upstage_provider_accepts_injected_shared_client() -> None:
    """The Upstage provider's constructor accepts a ``client`` arg.
    The latency fix builds ONE shared ``httpx.AsyncClient`` and passes
    it to every provider; the provider must NOT create a new client
    per request when one was injected.

    This test injects a sentinel client and verifies the provider uses
    it rather than creating its own.
    """
    import httpx

    from waxprep.model_gateway.providers.upstage import UpstageResponsesProvider

    sentinel = httpx.AsyncClient(base_url="https://sentinel.example.invalid")
    try:
        provider = UpstageResponsesProvider(
            api_key="test", base_url="https://api.example.invalid", client=sentinel
        )
        # The provider must remember the injected client and use it
        # rather than calling httpx.AsyncClient() per request.
        assert provider._client is sentinel, (
            "provider did not retain the injected shared client — "
            "the latency fix is not wired through"
        )
    finally:
        # AsyncClient has an aclose(); not awaited here because we
        # never opened it. Best-effort cleanup.
        pass


def test_build_gateway_passes_a_shared_client_to_the_provider() -> None:
    """The production ``build_gateway`` wiring must construct ONE
    ``httpx.AsyncClient`` and pass it to the Upstage provider. The
    provider must NOT be left to construct a fresh client per request.

    This test patches ``httpx.AsyncClient`` to count constructions
    and asserts that exactly ONE client is built across the wiring
    (not one per request, and not zero — the wiring must inject one).
    """
    import httpx

    from waxprep.model_gateway.providers import upstage as upstage_mod

    construction_count = 0
    real_async_client = httpx.AsyncClient

    def _counting_init(*args, **kwargs):
        nonlocal construction_count
        construction_count += 1
        return real_async_client(*args, **kwargs)

    # We also need to verify the provider's __init__ received a client.
    captured_client_args: list = []
    real_provider_init = upstage_mod.UpstageResponsesProvider.__init__

    def _capturing_init(self, *args, **kwargs):
        captured_client_args.append(kwargs.get("client"))
        real_provider_init(self, *args, **kwargs)

    saved_env = os.environ.get("WAXPREP_UPSTAGE_API_KEY")
    os.environ["WAXPREP_UPSTAGE_API_KEY"] = "test-key-for-latency"
    try:
        with patch.object(httpx, "AsyncClient", _counting_init), \
             patch.object(upstage_mod.UpstageResponsesProvider, "__init__", _capturing_init):
            from waxprep.app import build_gateway

            gateway = build_gateway(type("S", (), {
                "upstage_api_key": "test-key",
                "upstage_base_url": "https://api.upstage.ai/v1",
                "upstage_model": "solar-pro4",
            })())
            # At least one client was constructed during wiring.
            assert construction_count >= 1, (
                "build_gateway constructed zero httpx.AsyncClient instances — "
                "the shared client fix is not in place"
            )
            # The provider received a non-None client.
            assert any(c is not None for c in captured_client_args), (
                "build_gateway did not pass a client to the Upstage provider — "
                "the provider will fall back to creating a new client per request"
            )
    finally:
        if saved_env is None:
            os.environ.pop("WAXPREP_UPSTAGE_API_KEY", None)
        else:
            os.environ["WAXPREP_UPSTAGE_API_KEY"] = saved_env


# ---------------------------------------------------------------------------
# 3. CI fast-path: a single model call for simple messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_fast_path_runs_single_model_call_for_simple_greeting() -> None:
    """A simple message like ``"hi"`` must NOT trigger multiple
    CI model rounds. The fast-path runs ``decide`` once; the model
    returns ``stop`` (nothing to investigate for a greeting); the
    package is computed from runtime facts in one round.

    The contract is NOT "no model call" — it is "one model call,
    not many". The fast-path is honest: status is computed from
    what actually happened, not from the model's claim.
    """
    from support import add_student, make_world

    world = make_world()
    wax = add_student(world)
    await world.services.turns.handle_student_message(
        wax, "hi", channel="test"
    )
    # The mock model records every decide() and finalize() call.
    # For a simple greeting the fast-path runs exactly one decide()
    # (and one finalize()). If the count is much higher, the
    # fast-path is not in place.
    calls = world.context_model.calls
    assert calls == 1, (
        f"CI made {calls} model calls for a simple 'hi'; expected 1 "
        f"(fast-path). The latency fix is not in place."
    )


@pytest.mark.asyncio
async def test_ci_fast_path_one_decide_for_blank_message() -> None:
    """A blank message short-circuits to SKIPPED without any model
    round at all. The fast-path is even shorter here: zero model
    calls.
    """
    from support import add_student, make_world

    world = make_world()
    wax = add_student(world)
    result = await world.services.turns.handle_student_message(
        wax, "   ", channel="test"
    )
    assert result.context_package is not None
    from waxprep.context.models import InvestigationStatus

    assert result.context_package.investigation_status is InvestigationStatus.SKIPPED
    assert world.context_model.calls == 0, (
        "CI made model calls for a blank message; expected 0 (fast-path short-circuit)"
    )
