"""
backend/engine/private/sec/client.py
======================================
Official SEC EDGAR Fair-Access HTTP Client & Rate Limiter.

Core Principles:
    - SEC official guideline: TOTAL <= 10 requests / second regardless of machines.
    - Sentinax safety limit: Conservative local limiter <= 8 req/s (SEC_MAX_REQUESTS_PER_SECOND = 8).
    - Requires declared User-Agent string (e.g. "Sentinax <admin@example.com>").
    - Missing User-Agent raises ProviderConfigurationError on fetch without crashing the application on startup.
    - Strict typed error mapping for HTTP status codes (403, 404, 429, 5xx, timeouts, schema errors).
    - Transparent gzip/deflate decompression and json payload validation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

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
SEC_OFFICIAL_MAX_RPS = 10
DEFAULT_SENTINAX_SAFETY_RPS = 8.0


class SECRateLimiter:
    """
    Async token-bucket rate limiter enforcing SEC fair access guidelines.
    Guarantees request rate does not exceed configured rate per second.
    """
    def __init__(self, max_rps: float = DEFAULT_SENTINAX_SAFETY_RPS) -> None:
        if max_rps > SEC_OFFICIAL_MAX_RPS:
            logger.warning(
                f"Configured RPS {max_rps} exceeds official SEC limit of {SEC_OFFICIAL_MAX_RPS}. "
                f"Clamping to {SEC_OFFICIAL_MAX_RPS} req/s."
            )
            max_rps = float(SEC_OFFICIAL_MAX_RPS)
        self.rate = max_rps
        self.capacity = max_rps
        self.tokens = max_rps
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

            if self.tokens < 1.0:
                needed = 1.0 - self.tokens
                sleep_time = needed / self.rate
                await asyncio.sleep(sleep_time)
                self.tokens = 0.0
                self.last_update = time.monotonic()
            else:
                self.tokens -= 1.0


class SECEdgarClient:
    """
    HTTP client for official SEC EDGAR data APIs (data.sec.gov).
    """
    def __init__(
        self,
        user_agent: Optional[str] = None,
        max_rps: float = DEFAULT_SENTINAX_SAFETY_RPS,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._custom_user_agent = user_agent
        self.rate_limiter = SECRateLimiter(max_rps=max_rps)
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
        Executes a rate-limited, authorized GET request to SEC EDGAR and parses JSON response.
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
                f"SEC EDGAR access blocked (HTTP {resp.status_code}). Ensure declared User-Agent is valid.",
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
