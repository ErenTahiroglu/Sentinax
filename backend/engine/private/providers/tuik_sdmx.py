"""
backend/engine/private/providers/tuik_sdmx.py
===============================================
TÜİK (Turkish Statistical Institute) Official SDMX Web Service Adapter.

Official Specification:
    - Base URL: https://data.tuik.gov.tr/api/sdmx/v1/
    - Protocol: SDMX 2.1 RESTful Web Service (Official Data Portal active since June 2026).
    - Dataflows: Consumer Price Index (CPI / TÜFE) and Domestic Producer Price Index (D_PPI / Yİ-ÜFE).
    - Differentiates: Index levels (2003=100) vs Month-on-Month % change vs Year-on-Year % change.
    - Vergi Endeksleme Referansı: Yurt İçi Üretici Fiyat Endeksi (Yİ-ÜFE).
"""

from __future__ import annotations

import logging
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
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
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


class TUIKSDMXProvider(DataProviderContract):
    """
    Official data adapter for TÜİK SDMX REST Web Service.
    """
    provider_name: str = "TUIK_SDMX"
    provider_version: str = "1.0.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://data.tuik.gov.tr/api/sdmx/v1/data/"

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._http_client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        return self._http_client or get_http_client()

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Fetches inflation and price index data from official TÜİK SDMX web service.
        """
        series_key = context.provider_symbol or "TR_CPI_TUIK_YOY"
        canonical_def = MacroSeriesRegistry.get(series_key)

        dataflow = "CPI"
        if "PPI" in series_key:
            dataflow = "D_PPI"

        # Determine target period (YYYY-MM)
        target_date = context.effective_date or (context.as_of_time.date() if context.as_of_time else date.today())
        period_str = target_date.strftime("%Y-%m")

        endpoint_url = f"{self.base_url}{dataflow}/all"
        params = {
            "startPeriod": context.request_parameters.get("startPeriod") or period_str,
            "endPeriod": context.request_parameters.get("endPeriod") or period_str,
            "format": "json",
        }
        headers = {
            "Accept": "application/vnd.sdmx.data+json;version=1.0.0, application/json",
            "User-Agent": "Sentinax-Macro-Engine/1.0",
        }

        client = self._get_client()
        t_retrieved = datetime.now(timezone.utc)

        try:
            resp = await client.get(
                endpoint_url,
                params=params,
                headers=headers,
                timeout=15.0,
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"TÜİK SDMX request timed out: {e}", provider_name=self.provider_name)
        except httpx.NetworkError as e:
            raise ProviderServerError(f"TÜİK SDMX network error: {e}", provider_name=self.provider_name)

        if resp.status_code == 429:
            raise ProviderRateLimitError("TÜİK SDMX rate limit exceeded.", provider_name=self.provider_name)
        if resp.status_code >= 500:
            raise ProviderServerError(f"TÜİK SDMX server error: HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)
        if resp.status_code == 404:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=target_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"Dataflow {dataflow} not found for period {period_str}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        try:
            payload = resp.json()
        except Exception as e:
            raise ProviderSchemaError(f"Failed to parse TÜİK SDMX JSON response: {e}", provider_name=self.provider_name)

        normalized = self.normalize(payload)
        val = normalized.get("value")
        status = DataStatus.COMPLETE if val is not None else DataStatus.UNAVAILABLE

        # Extract published_at if provided in SDMX header/metadata
        published_at = self._extract_published_at(payload) or t_retrieved
        eff_date = self._parse_period_to_date(normalized.get("period")) or target_date

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=published_at,
            effective_date=eff_date,
            observed_at=t_retrieved,
            status=status,
            raw=payload,
            warnings=[] if status == DataStatus.COMPLETE else ["TÜİK SDMX payload contained no usable observation."],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """
        Maps SDMX dataset or tabular JSON to distinct canonical inflation fields:
        - cpi_index, cpi_yoy_pct, cpi_mom_pct
        - ppi_index, ppi_yoy_pct, ppi_mom_pct
        """
        if not isinstance(raw, (dict, list)):
            raise ProviderSchemaError("TÜİK payload must be dict or list.")

        # Case 1: Tabular / Simplified SDMX response array
        if isinstance(raw, list) and raw:
            item = raw[-1]
            period = item.get("PERIOD") or item.get("TIME_PERIOD")
            val = self._parse_decimal(item.get("VALUE") or item.get("OBS_VALUE"))
            indicator = str(item.get("INDICATOR") or "").upper()

            res = {
                "period": period,
                "value": val,
            }
            if "YOY" in indicator or "YILLIK" in indicator:
                res["yoy_pct"] = val
            elif "MOM" in indicator or "AYLIK" in indicator:
                res["mom_pct"] = val
            elif "INDEX" in indicator or "ENDEKS" in indicator:
                res["index_level"] = val
            return res

        # Case 2: Standard SDMX 2.1 Structure / Data Object
        if isinstance(raw, dict):
            # Check for direct key fields
            if "data" in raw or "dataSets" in raw:
                # Extract observations map
                obs_data = raw.get("data", raw)
                datasets = obs_data.get("dataSets", [{}])
                observations = datasets[0].get("observations", {}) if datasets else {}
                
                # If simplified series object exists
                series_dict = obs_data.get("series", {})
                period = raw.get("header", {}).get("extracted")

                # Find last observation value
                val = None
                if observations:
                    last_key = sorted(observations.keys())[-1]
                    raw_obs = observations[last_key]
                    val = self._parse_decimal(raw_obs[0] if isinstance(raw_obs, list) else raw_obs)

                return {
                    "period": period,
                    "value": val,
                    "raw_observations_count": len(observations),
                }

            # Direct dictionary fields
            period = raw.get("period") or raw.get("TIME_PERIOD")
            val = self._parse_decimal(raw.get("value") or raw.get("OBS_VALUE"))
            return {
                "period": period,
                "value": val,
                "cpi_index": self._parse_decimal(raw.get("cpi_index")),
                "cpi_yoy_pct": self._parse_decimal(raw.get("cpi_yoy_pct")),
                "cpi_mom_pct": self._parse_decimal(raw.get("cpi_mom_pct")),
                "ppi_index": self._parse_decimal(raw.get("ppi_index")),
                "ppi_yoy_pct": self._parse_decimal(raw.get("ppi_yoy_pct")),
                "ppi_mom_pct": self._parse_decimal(raw.get("ppi_mom_pct")),
            }

        return {}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        # Enforce that index level is not confused with percentage change
        cpi_idx = normalized.get("cpi_index")
        if cpi_idx is not None and cpi_idx < 10.0:
            warnings.append(f"CPI Index level appears abnormally low (< 10.0): {cpi_idx}")

        yoy = normalized.get("cpi_yoy_pct") or normalized.get("ppi_yoy_pct")
        if yoy is not None and yoy > 500.0:
            warnings.append(f"YoY inflation rate exceeds 500%: {yoy}")

        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=self.base_url,
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
        )

    @staticmethod
    def _extract_published_at(payload: Dict[str, Any]) -> Optional[datetime]:
        if not isinstance(payload, dict):
            return None
        header = payload.get("header", {})
        prepared = header.get("prepared") or header.get("extracted")
        if prepared:
            try:
                return datetime.fromisoformat(str(prepared).replace("Z", "+00:00"))
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_decimal(val: Any) -> Optional[float]:
        if val is None or val == "" or val == "-" or val == "null":
            return None
        try:
            return float(str(val).strip().replace(",", "."))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_period_to_date(period_str: Optional[str]) -> Optional[date]:
        if not period_str:
            return None
        try:
            # Format YYYY-MM
            cleaned = str(period_str).strip()
            if len(cleaned) == 7 and "-" in cleaned:
                year, month = map(int, cleaned.split("-"))
                # End of month date for inflation index
                if month in (1, 3, 5, 7, 8, 10, 12):
                    day = 31
                elif month in (4, 6, 9, 11):
                    day = 30
                else:
                    day = 29 if year % 4 == 0 else 28
                return date(year, month, day)
        except Exception:
            pass
        return None
