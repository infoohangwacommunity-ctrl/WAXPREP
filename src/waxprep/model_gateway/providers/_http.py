"""Shared HTTP error mapping for provider adapters."""

from __future__ import annotations

import httpx

from waxprep.model_gateway.errors import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
)


def raise_for_provider_status(response: httpx.Response) -> None:
    """Map HTTP status codes to normalized gateway errors."""
    if response.is_success:
        return
    text = response.text.lower()
    status = response.status_code
    if status in (401, 403):
        raise AuthenticationError(response.text)
    if status == 429:
        raise RateLimitError(response.text)
    if status in (408, 504):
        from waxprep.model_gateway.errors import TimeoutError as GatewayTimeout

        raise GatewayTimeout(response.text)
    if status >= 500:
        raise ProviderUnavailableError(response.text)
    if status == 400 and ("context" in text or "token" in text and "limit" in text):
        raise ContextLengthError(response.text)
    if 400 <= status < 500:
        raise InvalidRequestError(response.text)
    raise ProviderError(response.text)
