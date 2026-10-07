"""Provider error mapping tests."""

from __future__ import annotations

import httpx
import pytest

from waxprep.model_gateway.errors import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    ProviderUnavailableError,
    RateLimitError,
)
from waxprep.model_gateway.providers._http import raise_for_provider_status


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (429, RateLimitError),
        (503, ProviderUnavailableError),
        (400, InvalidRequestError),
    ],
)
def test_openai_error_mapping(status: int, expected: type[Exception]) -> None:
    response = httpx.Response(status, text="provider error")
    with pytest.raises(expected):
        raise_for_provider_status(response)


def test_context_error_mapping() -> None:
    response = httpx.Response(400, text="context length exceeded")
    with pytest.raises(ContextLengthError):
        raise_for_provider_status(response)
