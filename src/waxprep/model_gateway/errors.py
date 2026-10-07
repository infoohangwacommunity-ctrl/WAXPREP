"""Normalized model gateway errors."""

from __future__ import annotations


class ModelGatewayError(Exception):
    """Base class for model gateway failures."""

    retryable = False


class AuthenticationError(ModelGatewayError):
    """Provider authentication failed."""


class RateLimitError(ModelGatewayError):
    """Provider rate limit was reached."""

    retryable = True


class ContextLengthError(ModelGatewayError):
    """The request exceeded provider context limits."""


class InvalidRequestError(ModelGatewayError):
    """The provider rejected the request."""


class ProviderUnavailableError(ModelGatewayError):
    """The provider is temporarily unavailable."""

    retryable = True


class TimeoutError(ModelGatewayError):
    """The provider request timed out."""

    retryable = True


class UnsupportedCapabilityError(ModelGatewayError):
    """The requested capability is not available."""


class ProviderError(ModelGatewayError):
    """Unclassified provider-side failure."""

    retryable = False
