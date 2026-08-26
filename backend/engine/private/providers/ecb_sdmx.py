"""
backend/engine/private/providers/ecb_sdmx.py
=============================================
European Central Bank (ECB) Data Portal SDMX 2.1 Provider.

Official Specification:
    - Base URL: https://data-api.ecb.europa.eu/service/
    - Protocol: SDMX 2.1 RESTful Web Service
    - Entry Points:
        * Data: /data/{flowRef}/{key}
        * Dataflow: /dataflow/ECB
        * DataStructure: /datastructure/ECB/{structureRef}
    - Format: SDMX-CSV (format=csvdata) for deterministic and lightweight parsing.
    - Authentication: Open public service (No API key required).

Point-in-Time & Semantic Invariants:
    - Missing observations ("" or ".") are strictly parsed as None; 0.0 is preserved.
    - Current latest observation queries enforce `lastNObservations=1`.
    - Historical effective_date queries are bounded using `startPeriod` and `endPeriod`.
    - Historical SYSTEM_AS_OF and SOURCE_AS_OF are rejected (external PIT reconstruction unsupported).
    - updatedAfter timestamp is incremental delta metadata; NEVER fabricated into published_at.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx

from backend.engine.private.domain import (
    AsOfMode,
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


class ECBDataPortalProvider(DataProviderContract):
    """
    Adapter for European Central Bank (ECB) Data Portal SDMX 2.1 REST API.
    """
    provider_name: str = "ECB_DATA_PORTAL"
    provider_version: str = "1.0.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://data-api.ecb.europa.eu/service/"

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._http_client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        return self._http_client or get_http_client()

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Fetches macroeconomic observations from the ECB Data Portal.
        """
        # 1. Resolve series definition from registry or provider symbol
        canonical_def = None
        series_code = context.provider_symbol or ""

        if context.provider_symbol and context.provider_symbol.startswith("EA_"):
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
                series_code = canonical_def.provider_series_code

        if not series_code:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["No valid ECB series code or dataflow specified."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 2. Historical Mode Validation & Fail-Closed Guards
        if context.is_historical:
            if context.as_of_mode == AsOfMode.SYSTEM_AS_OF:
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
            elif context.as_of_mode == AsOfMode.SOURCE_AS_OF:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=None,
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=["ECB historical SOURCE_AS_OF requires local PIT storage; cannot be reconstructed via external API."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )
            else:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=None,
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"Unhandled historical as_of_mode: '{context.as_of_mode}'. Fail closed."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )

        # 3. Parse dataflow and series key (Format: FLOW_REF/SERIES_KEY or FLOW.KEY)
        flow_ref, key = self._split_flow_and_key(series_code)
        if not flow_ref or not key:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"Invalid ECB series code format: '{series_code}'. Expected 'FLOW/KEY'."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 4. Build query parameters (Bounded requests)
        params: Dict[str, Any] = {
            "format": "csvdata",
        }

        if context.effective_date:
            period_str = context.effective_date.strftime("%Y-%m-%d")
            params["startPeriod"] = period_str
            params["endPeriod"] = period_str
        else:
            params["lastNObservations"] = 1

        endpoint_url = f"{self.base_url}data/{flow_ref}/{key}"
        client = self._get_client()
        t_retrieved = datetime.now(timezone.utc)

        try:
            resp = await client.get(
                endpoint_url,
                params=params,
                timeout=12.0,
                headers={"Accept": "text/csv"},
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"ECB API request timed out: {e}", provider_name=self.provider_name)
        except httpx.NetworkError as e:
            raise ProviderServerError(f"ECB API network error: {e}", provider_name=self.provider_name)

        if resp.status_code in (400, 404):
            raise ProviderInvalidSymbolError(f"ECB series '{flow_ref}/{key}' not found or parameters invalid.", provider_name=self.provider_name)
        if resp.status_code in (401, 403):
            raise ProviderAuthenticationError("ECB API access forbidden.", provider_name=self.provider_name)
        if resp.status_code == 429:
            raise ProviderRateLimitError("ECB API rate limit exceeded.", provider_name=self.provider_name)
        if resp.status_code >= 500:
            raise ProviderServerError(f"ECB server error: HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)
        if resp.status_code != 200:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"ECB returned HTTP {resp.status_code}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 5. Parse SDMX-CSV response
        csv_text = resp.text
        rows = self._parse_sdmx_csv(csv_text)
        if not rows:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw={"csv_text": csv_text, "rows": []},
                warnings=["ECB returned 0 observations for requested query."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        latest_row = rows[-1]
        raw_val = latest_row.get("OBS_VALUE")
        parsed_val = self._parse_decimal(raw_val)

        time_period_str = latest_row.get("TIME_PERIOD")
        eff_date = self._parse_time_period(time_period_str)

        if eff_date is None:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw={"csv_text": csv_text, "rows": rows},
                warnings=[f"ECB observation date was unparseable: '{time_period_str}'."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        status = DataStatus.COMPLETE if parsed_val is not None else DataStatus.UNAVAILABLE

        # Quote direction & metadata
        quote_dir = None
        if flow_ref == "EXR" and "USD" in key:
            quote_dir = "USD per 1 EUR"

        source_meta: Dict[str, Any] = {
            "delivery_provider": "European Central Bank Data Portal",
            "source_role": "CENTRAL_BANK",
            "origin_source": canonical_def.origin_source if canonical_def else "European Central Bank",
            "release_name": canonical_def.release_name if canonical_def else "ECB Statistical Data Release",
            "dataflow": flow_ref,
            "series_key": key,
            "quote_direction": quote_dir,
            "obs_status": latest_row.get("OBS_STATUS"),
            "obs_conf": latest_row.get("OBS_CONF"),
            "source_available_date": None,
            "availability_precision": None,
        }

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=None,             # Invariant: Never fabricate publication timestamp
            effective_date=eff_date,
            observed_at=t_retrieved,
            status=status,
            raw={"csv_text": csv_text, "rows": rows, "latest": latest_row},
            warnings=[] if status == DataStatus.COMPLETE else [f"ECB observation value is missing (raw: '{raw_val}')."],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
            source_metadata=source_meta,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """
        Normalizes ECB raw payload to canonical fields.
        """
        if isinstance(raw, dict):
            latest = raw.get("latest", {})
            raw_val = latest.get("OBS_VALUE")
            time_period = latest.get("TIME_PERIOD")
        else:
            return {}

        return {
            "time_period": time_period,
            "value": self._parse_decimal(raw_val),
            "obs_status": latest.get("OBS_STATUS"),
        }

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return []

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        meta = dict(response.source_metadata)
        meta["delivery_provider"] = "European Central Bank Data Portal"
        meta["source_role"] = "CENTRAL_BANK"
        if response.provider_symbol and not meta.get("origin_source"):
            canonical_def = MacroSeriesRegistry.get(response.provider_symbol)
            if canonical_def:
                meta["origin_source"] = canonical_def.origin_source
                meta["release_name"] = canonical_def.release_name

        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=f"{self.base_url}data",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=meta,
        )

    async def get_dataflows(self) -> List[Dict[str, Any]]:
        """
        Discovery helper: Fetches official ECB dataflows list.
        """
        client = self._get_client()
        try:
            resp = await client.get(f"{self.base_url}dataflow/ECB", headers={"Accept": "application/json"}, timeout=12.0)
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"ECB dataflow discovery timed out: {e}", provider_name=self.provider_name)
        except httpx.NetworkError as e:
            raise ProviderServerError(f"ECB dataflow discovery network error: {e}", provider_name=self.provider_name)

        if resp.status_code != 200:
            raise ProviderServerError(f"ECB dataflow discovery failed: HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)

        try:
            payload = resp.json()
        except Exception as e:
            raise ProviderSchemaError(f"Malformed dataflow JSON: {e}", provider_name=self.provider_name)

        # Extract dataflow summaries
        structures = payload.get("data", {}).get("dataflows", [])
        return structures

    @staticmethod
    def _split_flow_and_key(series_code: str) -> Tuple[Optional[str], Optional[str]]:
        if "/" in series_code:
            parts = series_code.split("/", 1)
            return parts[0].strip(), parts[1].strip()
        elif series_code.startswith("EXR.") or series_code.startswith("FM.") or series_code.startswith("EST."):
            parts = series_code.split(".", 1)
            return parts[0].strip(), parts[1].strip()
        return None, None

    @staticmethod
    def _parse_sdmx_csv(csv_text: str) -> List[Dict[str, str]]:
        if not csv_text or not csv_text.strip():
            return []
        f = io.StringIO(csv_text.strip())
        reader = csv.DictReader(f)
        return list(reader)

    @staticmethod
    def _parse_decimal(val_str: Any) -> Optional[float]:
        if val_str is None:
            return None
        cleaned = str(val_str).strip()
        if cleaned in ("", ".", "-", "NaN", "null", "None"):
            return None
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_time_period(period_str: Optional[str]) -> Optional[date]:
        if not period_str:
            return None
        cleaned = str(period_str).strip()
        # 1. Daily format: YYYY-MM-DD
        if len(cleaned) == 10 and cleaned.count("-") == 2:
            try:
                return datetime.strptime(cleaned, "%Y-%m-%d").date()
            except ValueError:
                pass
        # 2. Monthly format: YYYY-MM
        if len(cleaned) == 7 and cleaned.count("-") == 1:
            try:
                return datetime.strptime(f"{cleaned}-01", "%Y-%m-%d").date()
            except ValueError:
                pass
        # 3. Quarterly format: YYYY-Q#
        if "Q" in cleaned:
            parts = cleaned.split("-Q" if "-Q" in cleaned else "Q")
            if len(parts) == 2:
                try:
                    year = int(parts[0])
                    quarter = int(parts[1])
                    month = (quarter - 1) * 3 + 1
                    return date(year, month, 1)
                except ValueError:
                    pass
        return None
