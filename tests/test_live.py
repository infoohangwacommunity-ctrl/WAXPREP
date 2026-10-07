"""Boundary for future paid AI-provider integration tests."""

import os

import pytest


@pytest.mark.live
def test_live_provider_suite_requires_explicit_opt_in() -> None:
    """Paid tests must never run accidentally."""
    if os.getenv("WAXPREP_RUN_LIVE_TESTS") != "1":
        pytest.skip("Paid provider tests require explicit opt-in.")

    # The real provider test will be implemented in the provider build.
    pytest.skip("No AI provider is implemented in Foundation.")
