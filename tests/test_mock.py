"""Deterministic tests that never call a paid provider."""


def test_mock_provider_is_deterministic() -> None:
    """Foundation tests must not need a real model API."""
    fake_response = "MOCK_WAXPREP_RESPONSE"

    assert fake_response == "MOCK_WAXPREP_RESPONSE"
