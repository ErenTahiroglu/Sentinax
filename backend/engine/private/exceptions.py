"""
backend/engine/private/exceptions.py
======================================
Typed Exception Taxonomy for the Provider & Data Ingestion Layer.

Core Principles:
    - Eliminates fragile string pattern-matching in retry logic.
    - Clean distinction between Transient (Retryable) and NonRetryable errors.
    - Fast-fail non-retryable errors (auth, permissions, bad symbols, schema errors).
"""

from typing import Optional


class ProviderError(Exception):
    """Base exception for all provider-related failures."""

    def __init__(self, message: str, provider_name: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.provider_name = provider_name

    def __str__(self) -> str:
        prefix = f"[{self.provider_name}] " if self.provider_name else ""
        return f"{prefix}{self.message}"


# ─────────────────────────────────────────────────────────────────────────────
# Retryable / Transient Errors
# ─────────────────────────────────────────────────────────────────────────────

class TransientProviderError(ProviderError):
    """Base class for transient, retryable provider errors."""
    pass


class ProviderTimeoutError(TransientProviderError):
    """Network connection or execution timeout."""
    pass


class ProviderRateLimitError(TransientProviderError):
    """HTTP 429 Too Many Requests or rate limiter budget exhausted."""
    def __init__(self, message: str = "Rate limit exceeded", retry_after_seconds: Optional[float] = None, provider_name: Optional[str] = None) -> None:
        super().__init__(message, provider_name=provider_name)
        self.retry_after_seconds = retry_after_seconds


class ProviderServerError(TransientProviderError):
    """HTTP 5xx (500, 502, 503, 504) server-side failure."""
    def __init__(self, message: str, status_code: int = 500, provider_name: Optional[str] = None) -> None:
        super().__init__(message, provider_name=provider_name)
        self.status_code = status_code


class ProviderConnectionError(TransientProviderError):
    """Temporary socket drop, connection reset, or DNS failure."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Non-Retryable / Fatal Errors (Fail Fast)
# ─────────────────────────────────────────────────────────────────────────────

class NonRetryableProviderError(ProviderError):
    """Base class for non-retryable provider errors (fail fast to next fallback)."""
    pass


class ProviderAuthenticationError(NonRetryableProviderError):
    """HTTP 401 Unauthorized / Invalid API Key / Expired token."""
    pass


class ProviderPermissionError(NonRetryableProviderError):
    """HTTP 403 Forbidden / License plan limitation."""
    pass


class ProviderInvalidSymbolError(NonRetryableProviderError):
    """HTTP 404 Not Found / Unknown instrument identifier on provider."""
    pass


class ProviderSchemaError(NonRetryableProviderError):
    """Malformed response payload or schema validation failure."""
    pass


class ProviderConfigurationError(NonRetryableProviderError):
    """Misconfigured provider settings or missing required credentials."""
    pass


class ProviderLookaheadError(NonRetryableProviderError):
    """Observation timestamp occurs after requested point-in-time boundary (lookahead violation)."""
    pass
