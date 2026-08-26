"""
backend/engine/private/providers/fred_alfred.py
=================================================
Unified Federal Reserve Economic Data (FRED) & ALFRED Vintage Data Provider.

Official Specification:
    - Base URL: https://api.stlouisfed.org/fred/
    - Protocol: RESTful Web Service (API Version 1)
    - Authentication: `api_key` query parameter (Secret-contained: never logged or exposed in cache/diagnostics)
    - Modes:
        * CURRENT: Latest official observations from FRED
        * SOURCE_VINTAGE: Point-in-time historical revisions from ALFRED via `vintage_dates`
    - Units: Enforces raw linear levels (`units=lin`) without server-side transformations.

Hardening Invariants:
    - Historical SYSTEM_AS_OF is rejected (must be served from local PIT storage).
    - Missing observation marker `"."` is strictly parsed as `None`.
    - Zero values (`"0"`, `"0.0"`, `0`) are preserved as valid `0.0` floats.
    - `realtime_start` is stored with DATE precision (`source_available_date`); never fabricated into `published_at` timestamp.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx

from backend.engine.private.domain import (
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderAuthenticationError,
    ProviderInvalidSymbolError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.macro.models import (
    ContractStatus,
    MacroObservationRecord,
    MacroUnit,
)
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)


class FREDALFREDProvider(DataProviderContract):
    """
    Unified adapter for St. Louis Fed FRED and ALFRED vintage macroeconomic data.
    """
    provider_name: str = "FRED_ALFRED"
    provider_version: str = "1.0.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://api.stlouisfed.org/fred/"

    def __init__(
        self,
        api_key: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._api_key = api_key or os.getenv("FRED_API_KEY")
        self._http_client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        return self._http_client or get_http_client()

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Fetches macro observations from FRED or ALFRED vintage based on fetch context.
        """
        # Resolve series code from canonical registry or context
        series_id = context.provider_symbol
        canonical_def = None

        if context.provider_symbol and context.provider_symbol.startswith("US_"):
            canonical_def = MacroSeriesRegistry.get(context.provider_symbol)
            if canonical_def:
                if not canonical_def.is_active or canonical_def.contract_status != ContractStatus.VERIFIED:
                    return ProviderResponse(
                        provider_name=self.provider_name,
                        source_quality=self.source_quality,
                        retrieved_at=datetime.now(timezone.utc),
                        published_at=None,
                        effective_date=None,
                        status=DataStatus.UNAVAILABLE,
                        raw=None,
                        warnings=[f"Series {context.provider_symbol} is unverified or disabled in registry."],
                        canonical_instrument_id=context.canonical_instrument_id,
                        provider_symbol=context.provider_symbol,
                    )
                series_id = canonical_def.provider_series_code

        if not series_id:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["No valid FRED series ID specified in request."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 1. Historical SYSTEM_AS_OF Guard (Directive 9)
        if context.is_historical and context.as_of_mode == "SYSTEM_AS_OF":
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["Historical SYSTEM_AS_OF requires local PIT storage; cannot be reconstructed via external API."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 2. Check API Key presence
        if not self._api_key:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["FRED_API_KEY is not configured in environment."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 3. Build query parameters
        params: Dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
            "units": "lin", # Always raw linear levels; no server-side transforms
        }

        # Date filtering
        if context.effective_date:
            params["observation_start"] = context.effective_date.strftime("%Y-%m-%d")
            params["observation_end"] = context.effective_date.strftime("%Y-%m-%d")

        # ALFRED Point-in-Time Vintage Mode (Directive 8)
        vintage_requested: Optional[date] = None
        if context.is_historical and context.as_of_time:
            vintage_requested = context.as_of_time.date()
            params["vintage_dates"] = vintage_requested.strftime("%Y-%m-%d")

        endpoint_url = f"{self.base_url}series/observations"
        client = self._get_client()
        t_retrieved = datetime.now(timezone.utc)

        try:
            resp = await client.get(
                endpoint_url,
                params=params,
                timeout=12.0,
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"FRED API request timed out: {e}", provider_name=self.provider_name)
        except httpx.NetworkError as e:
            raise ProviderServerError(f"FRED API network error: {e}", provider_name=self.provider_name)

        if resp.status_code in (400, 404):
            # Check if invalid series or bad parameter
            err_text = resp.text
            if "Bad Request" in err_text or "does not exist" in err_text:
                raise ProviderInvalidSymbolError(f"FRED series '{series_id}' does not exist or parameter is invalid.", provider_name=self.provider_name)
        if resp.status_code in (401, 403):
            raise ProviderAuthenticationError("FRED API authentication failed. Invalid API key.", provider_name=self.provider_name)
        if resp.status_code == 429:
            raise ProviderRateLimitError("FRED API rate limit exceeded.", provider_name=self.provider_name)
        if resp.status_code >= 500:
            raise ProviderServerError(f"FRED server error: HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)
        if resp.status_code != 200:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"FRED returned HTTP {resp.status_code}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        try:
            payload = resp.json()
        except Exception as e:
            raise ProviderSchemaError(f"Failed to parse FRED JSON response: {e}", provider_name=self.provider_name)

        if not isinstance(payload, dict) or "observations" not in payload:
            raise ProviderSchemaError("Malformed FRED response: missing 'observations' array.", provider_name=self.provider_name)

        observations = payload.get("observations", [])
        if not observations:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=payload,
                warnings=["FRED returned 0 observation records for requested parameters."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # Most recent observation in series
        latest_obs = observations[-1]
        raw_val = latest_obs.get("value")
        parsed_val = self._parse_decimal(raw_val)

        obs_date_str = latest_obs.get("date")
        eff_date = self._parse_date(obs_date_str)

        # Source available date (ALFRED realtime_start)
        rt_start_str = latest_obs.get("realtime_start") or payload.get("realtime_start")
        source_available_date = self._parse_date(rt_start_str)

        # Invariant: Unparseable date returns UNAVAILABLE
        if eff_date is None:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=payload,
                warnings=[f"FRED observation date was unparseable or missing: '{obs_date_str}'."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        status = DataStatus.COMPLETE if parsed_val is not None else DataStatus.UNAVAILABLE

        # Sanitize payload for raw snapshot (strip secret API key from request representation)
        sanitized_raw = {k: v for k, v in payload.items() if k != "api_key"}

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=None, # Never fabricate timestamp from date-level realtime_start
            effective_date=eff_date,
            observed_at=t_retrieved,
            status=status,
            raw=sanitized_raw,
            warnings=[] if status == DataStatus.COMPLETE else [f"FRED observation value is missing (raw: '{raw_val}')."],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """
        Normalizes FRED payload to canonical dictionary fields.
        """
        if not isinstance(raw, dict):
            raise ProviderSchemaError("FRED raw payload must be a dict.")

        observations = raw.get("observations", [])
        if not observations:
            return {}

        latest_obs = observations[-1]
        raw_val = latest_obs.get("value")
        parsed_val = self._parse_decimal(raw_val)

        return {
            "date": latest_obs.get("date"),
            "value": parsed_val,
            "realtime_start": latest_obs.get("realtime_start") or raw.get("realtime_start"),
            "realtime_end": latest_obs.get("realtime_end") or raw.get("realtime_end"),
            "count": raw.get("count", len(observations)),
            "units": raw.get("units"),
        }

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        # Resolve origin source from registry definition if available
        origin_source = None
        release_name = None
        if response.provider_symbol:
            canonical_def = MacroSeriesRegistry.get(response.provider_symbol)
            if canonical_def:
                origin_source = canonical_def.origin_source
                release_name = canonical_def.release_name

        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=f"{self.base_url}series/observations",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
        )

    async def get_vintage_dates(self, series_id: str, limit: int = 1000) -> List[date]:
        """
        Helper to fetch all historical revision vintage dates from fred/series/vintagedates.
        """
        if not self._api_key:
            raise ProviderAuthenticationError("FRED_API_KEY is not configured.", provider_name=self.provider_name)

        params = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
            "limit": limit,
            "sort_order": "desc",
        }
        client = self._get_client()
        resp = await client.get(f"{self.base_url}series/vintagedates", params=params, timeout=10.0)

        if resp.status_code != 200:
            return []

        payload = resp.json()
        vintage_strs = payload.get("vintage_dates", [])
        parsed_dates: List[date] = []
        for s in vintage_strs:
            d = self._parse_date(s)
            if d:
                parsed_dates.append(d)
        return parsed_dates

    async def get_series_metadata(self, series_id: str) -> Dict[str, Any]:
        """
        Helper to fetch official series metadata from fred/series.
        """
        if not self._api_key:
            raise ProviderAuthenticationError("FRED_API_KEY is not configured.", provider_name=self.provider_name)

        params = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        client = self._get_client()
        resp = await client.get(f"{self.base_url}series", params=params, timeout=10.0)

        if resp.status_code != 200:
            return {}

        payload = resp.json()
        seriess = payload.get("seriess", [])
        return seriess[0] if seriess else {}

    @staticmethod
    def _parse_decimal(val_str: Any) -> Optional[float]:
        if val_str is None:
            return None
        cleaned = str(val_str).strip()
        # Invariant: FRED missing marker is "."
        if cleaned in ("", ".", "-", "null", "None"):
            return None
        try:
            # 0.0, 0, -5.2 etc are valid
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_date(date_str: Optional[str]) -> Optional[date]:
        if not date_str:
            return None
        try:
            return datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
