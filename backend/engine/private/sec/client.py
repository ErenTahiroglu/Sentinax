"""
backend/engine/private/sec/client.py
======================================
Official SEC EDGAR Fair-Access HTTP Client & Process-Wide Rate Limiter.

Core Hardening Invariants:
    - SEC official guideline: TOTAL <= 10 requests / second regardless of machines.
    - Sentinax safety limit: Conservative local limiter <= 8 req/s (DEFAULT_SENTINAX_SAFETY_RPS = 8.0).
    - Serialized leaky pacing: min_interval = 1.0 / rps (burst capacity = 1) prevents initial multi-request bursts.
    - Process-wide singleton rate limiter shared across all SECEdgarClient instances, submissions, and facts.
    - Invalid RPS (<=0, NaN, Inf) is strictly rejected.
    - Missing User-Agent raises ProviderConfigurationError on fetch without crashing application boot.
    - Typed error mapping for 403 (Permission/Block), 404, 429 (Rate Limit), 5xx, timeouts, and schema errors.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any, Callable, Dict, Optional

import httpx

from backend.engine.private.exceptions import (
    ProviderConfigurationError,
    ProviderInvalidSymbolError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)

# SEC Fair Access Invariants
SEC_OFFICIAL_MAX_RPS = 10.0
DEFAULT_SENTINAX_SAFETY_RPS = 8.0


class SECRateLimiter:
    """
    Async leaky pacing rate limiter enforcing serialized request spacing.
    Guarantees that consecutive requests are spaced by at least (1.0 / max_rps) seconds (burst capacity = 1).
    """
    def __init__(
        self,
        max_rps: float = DEFAULT_SENTINAX_SAFETY_RPS,
        time_func: Optional[Callable[[], float]] = None,
        sleep_func: Optional[Callable[[float], Any]] = None,
    ) -> None:
        if math.isnan(max_rps) or math.isinf(max_rps) or max_rps <= 0:
            raise ValueError(f"Invalid max_rps: {max_rps}. Must be a positive finite number.")

        if max_rps > SEC_OFFICIAL_MAX_RPS:
            logger.warning(
                f"Configured RPS {max_rps} exceeds official SEC limit of {SEC_OFFICIAL_MAX_RPS}. "
                f"Clamping to {SEC_OFFICIAL_MAX_RPS} req/s."
            )
            max_rps = float(SEC_OFFICIAL_MAX_RPS)

        self.rate = float(max_rps)
        self.min_interval = 1.0 / self.rate
        self._time_func = time_func or time.monotonic
        self._sleep_func = sleep_func or asyncio.sleep
        self.last_request_time: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._time_func()
            time_since_last = now - self.last_request_time
            if time_since_last < self.min_interval:
                sleep_needed = self.min_interval - time_since_last
                await self._sleep_func(sleep_needed)
                self.last_request_time = self._time_func()
            else:
                self.last_request_time = now


# Process-wide shared singleton rate limiter instance
_SHARED_SEC_LIMITER: Optional[SECRateLimiter] = None


def get_shared_sec_rate_limiter(max_rps: float = DEFAULT_SENTINAX_SAFETY_RPS) -> SECRateLimiter:
    global _SHARED_SEC_LIMITER
    if _SHARED_SEC_LIMITER is None:
        _SHARED_SEC_LIMITER = SECRateLimiter(max_rps=max_rps)
    return _SHARED_SEC_LIMITER


def reset_shared_sec_rate_limiter(limiter: Optional[SECRateLimiter] = None) -> None:
    """Utility for tests to inject custom mock timing limiters."""
    global _SHARED_SEC_LIMITER
    _SHARED_SEC_LIMITER = limiter


class SECEdgarClient:
    """
    HTTP client for official SEC EDGAR data APIs (data.sec.gov).
    Uses the process-wide shared rate limiter by default.
    """
    def __init__(
        self,
        user_agent: Optional[str] = None,
        max_rps: float = DEFAULT_SENTINAX_SAFETY_RPS,
        http_client: Optional[httpx.AsyncClient] = None,
        rate_limiter: Optional[SECRateLimiter] = None,
    ) -> None:
        self._custom_user_agent = user_agent
        self.rate_limiter = rate_limiter or get_shared_sec_rate_limiter(max_rps=max_rps)
        self._http_client = http_client

    def get_user_agent(self) -> str:
        agent = self._custom_user_agent or os.getenv("SEC_USER_AGENT", "").strip()
        if not agent:
            raise ProviderConfigurationError(
                "SEC_USER_AGENT is not configured. SEC EDGAR fair access policy requires "
                "a declared User-Agent header (e.g. 'Sentinax <admin@example.com>').",
                provider_name="SEC_EDGAR",
            )
        return agent

    def _get_client(self) -> httpx.AsyncClient:
        return self._http_client or get_http_client()

    async def get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """
        Executes a serialized rate-limited GET request to SEC EDGAR and parses JSON response.
        """
        user_agent = self.get_user_agent()
        await self.rate_limiter.acquire()

        headers = {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        }

        client = self._get_client()
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"SEC EDGAR request timed out for {url}: {e}", provider_name="SEC_EDGAR")
        except httpx.NetworkError as e:
            raise ProviderServerError(f"SEC EDGAR network error for {url}: {e}", provider_name="SEC_EDGAR")

        # Status Code Error Handling
        if resp.status_code in (401, 403):
            raise ProviderPermissionError(
                f"SEC EDGAR access blocked (HTTP {resp.status_code}). Fair-access or user-agent policy restriction.",
                provider_name="SEC_EDGAR",
            )
        if resp.status_code == 404:
            raise ProviderInvalidSymbolError(
                f"Resource not found on SEC EDGAR (HTTP 404): {url}",
                provider_name="SEC_EDGAR",
            )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            retry_s = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise ProviderRateLimitError(
                "SEC EDGAR rate limit exceeded (HTTP 429).",
                retry_after_seconds=retry_s,
                provider_name="SEC_EDGAR",
            )
        if resp.status_code >= 500:
            raise ProviderServerError(
                f"SEC EDGAR server error: HTTP {resp.status_code}",
                status_code=resp.status_code,
                provider_name="SEC_EDGAR",
            )
        if resp.status_code != 200:
            raise ProviderServerError(
                f"SEC EDGAR returned unexpected HTTP {resp.status_code}",
                status_code=resp.status_code,
                provider_name="SEC_EDGAR",
            )

        try:
            return resp.json()
        except Exception as e:
            raise ProviderSchemaError(
                f"Failed to parse JSON response from SEC EDGAR ({url}): {e}",
                provider_name="SEC_EDGAR",
            )
