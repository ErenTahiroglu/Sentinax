"""
backend/engine/private/providers/tcmb_evds.py
===============================================
TCMB EVDS (Electronic Data Delivery System) Official API Adapter.

Official Specification:
    - Base URL: https://evds2.tcmb.gov.tr/service/evds/
    - Authentication: API key provided via HTTP Request Header `key` (NEVER in URL/logs).
    - Query Parameters: `series`, `startDate`, `endDate`, `type=json`
    - Date Format: DD-MM-YYYY
    - Response Format: JSON object containing items array with series fields (dots converted to underscores).

Hardening Invariants:
    - Zero observation is a valid float (0.0), NEVER treated as missing or falsy.
    - Missing observation is None.
    - Multi-series returns deterministic `values` dictionary; does not overwrite `value`.
    - Unparseable response date returns UNAVAILABLE (no fabricated date fallback).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
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
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.macro.models import ContractStatus
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)


class TCMBEVDSProvider(DataProviderContract):
    """
    Official data adapter for Turkey Central Bank EVDS Web Service.
    """
    provider_name: str = "TCMB_EVDS"
    provider_version: str = "1.0.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://evds2.tcmb.gov.tr/service/evds/"

    def __init__(
        self,
        api_key: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """
        Initializes EVDS provider.
        Key is read from argument or environment variable `TCMB_EVDS_API_KEY`.
        """
        self._api_key = api_key or os.getenv("TCMB_EVDS_API_KEY")
        self._client: Optional[httpx.AsyncClient] = http_client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or getattr(self._client, "is_closed", False) is True:
            self._client = get_http_client()
        return self._client

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Executes HTTP request against EVDS official JSON API.
        Enforces:
            - Header authentication
            - Bounded date formatting
            - Point-in-time retrieved timestamps
            - Non-zero status checks
            - Series verification checks before issuing requests
        """
        # Resolve series code from canonical registry or context
        series_code = context.provider_symbol
        is_pm = False
        if context.provider_symbol and context.provider_symbol.startswith("TR_"):
            canonical_def = MacroSeriesRegistry.get(context.provider_symbol)
            if canonical_def and canonical_def.provider == self.provider_name:
                if canonical_def.contract_status == ContractStatus.DISABLED or not canonical_def.is_active:
                    return ProviderResponse(
                        provider_name=self.provider_name,
                        source_quality=self.source_quality,
                        retrieved_at=datetime.now(timezone.utc),
                        published_at=None,
                        effective_date=None,
                        status=DataStatus.UNAVAILABLE,
                        raw=None,
                        warnings=[f"Series {context.provider_symbol} is disabled."],
                        canonical_instrument_id=context.canonical_instrument_id,
                        provider_symbol=context.provider_symbol,
                    )
                series_code = canonical_def.provider_series_code
        elif context.provider_symbol:
            from backend.engine.private.precious_metals.models import SeriesVerificationStatus
            from backend.engine.private.precious_metals.registry import PreciousMetalSeriesRegistry
            pm_def = PreciousMetalSeriesRegistry.get(context.provider_symbol)
            if pm_def:
                is_pm = True
                if pm_def.verification_status != SeriesVerificationStatus.VERIFIED or not pm_def.is_active:
                    return ProviderResponse(
                        provider_name=self.provider_name,
                        source_quality=self.source_quality,
                        retrieved_at=datetime.now(timezone.utc),
                        published_at=None,
                        effective_date=None,
                        status=DataStatus.UNAVAILABLE,
                        raw=None,
                        warnings=[f"Precious metals series '{context.provider_symbol}' is unverified or disabled in registry."],
                        canonical_instrument_id=context.canonical_instrument_id,
                        provider_symbol=context.provider_symbol,
                    )
                series_code = pm_def.series_code

        if not series_code:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["No valid EVDS series code specified in request."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # Check API Key presence (graceful UNAVAILABLE if missing)
        if not self._api_key:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["TCMB_EVDS_API_KEY is not configured in environment."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # Format date filters
        target_date = context.effective_date or (context.as_of_time.date() if context.as_of_time else date.today())
        start_str = context.request_parameters.get("startDate") or target_date.strftime("%d-%m-%Y")
        end_str = context.request_parameters.get("endDate") or target_date.strftime("%d-%m-%Y")

        params = {
            "series": series_code,
            "startDate": start_str,
            "endDate": end_str,
            "type": "json",
        }

        # Header authentication: NEVER pass key in query params
        headers = {
            "key": self._api_key,
            "Accept": "application/json",
        }

        client = self._get_client()
        t_retrieved = datetime.now(timezone.utc)

        try:
            resp = await client.get(
                self.base_url,
                params=params,
                headers=headers,
                timeout=10.0,
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"TCMB EVDS request timed out: {e}", provider_name=self.provider_name)
        except httpx.NetworkError as e:
            raise ProviderServerError(f"TCMB EVDS network error: {e}", provider_name=self.provider_name)

        if resp.status_code in (401, 403):
            raise ProviderAuthenticationError("TCMB EVDS authentication failed. Invalid API key.", provider_name=self.provider_name)
        if resp.status_code == 429:
            raise ProviderRateLimitError("TCMB EVDS rate limit exceeded.", provider_name=self.provider_name)
        if resp.status_code >= 500:
            raise ProviderServerError(f"TCMB EVDS server error: HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)
        if resp.status_code != 200:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"EVDS returned HTTP {resp.status_code}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        try:
            payload = resp.json()
        except Exception as e:
            raise ProviderSchemaError(f"Failed to parse EVDS JSON payload: {e}", provider_name=self.provider_name)

        # EVDS error structure in JSON
        if isinstance(payload, dict) and "error" in payload:
            err_msg = str(payload["error"])
            if "key" in err_msg.lower() or "auth" in err_msg.lower():
                raise ProviderAuthenticationError(f"EVDS auth error: {err_msg}", provider_name=self.provider_name)
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=payload,
                warnings=[f"EVDS error: {err_msg}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not items:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=payload,
                warnings=["EVDS returned 0 observation items for query period."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        latest_item = items[-1]
        item_date_str = latest_item.get("Tarih")
        eff_date = self._parse_date(item_date_str)

        # Invariant: No fabricated effective_date
        if eff_date is None:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=payload,
                warnings=[f"EVDS response item contained missing or unparseable Tarih field: '{item_date_str}'"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        normalized = self.normalize(payload, is_precious_metal=is_pm)
        is_multi = "-" in series_code

        if is_multi:
            # Multi-series: check if at least one value is present
            values_dict = normalized.get("values", {})
            has_valid = any(v is not None for v in values_dict.values())
            status = DataStatus.COMPLETE if has_valid else DataStatus.UNAVAILABLE
        else:
            val = normalized.get("value")
            status = DataStatus.COMPLETE if val is not None else DataStatus.UNAVAILABLE

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=None,
            effective_date=eff_date,
            observed_at=t_retrieved,
            status=status,
            raw=payload,
            warnings=[] if status == DataStatus.COMPLETE else ["Value in EVDS observation was null, non-existent, or unparseable."],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any, is_precious_metal: bool = False) -> Dict[str, Any]:
        """
        Normalizes raw EVDS payload to canonical macro or precious metal fields.
        For precious metals, parses values as exact Decimal (no float conversion).
        Differentiates single-series (returns 'value') vs multi-series (returns 'values' dict).
        """
        if not isinstance(raw, dict):
            raise ProviderSchemaError("EVDS raw payload must be a dict.")

        items = raw.get("items", [])
        if not items:
            return {}

        latest_item = items[-1]
        normalized: Dict[str, Any] = {
            "date": latest_item.get("Tarih"),
            "unix_time": latest_item.get("UNIXTIME"),
        }

        # Extract series fields
        series_fields: Dict[str, Any] = {}
        for k, v in latest_item.items():
            if k not in ("Tarih", "UNIXTIME"):
                if is_precious_metal:
                    parsed = self._parse_exact_decimal(v)
                else:
                    parsed = self._parse_decimal(v)
                series_fields[k] = parsed

        if len(series_fields) == 1:
            # Single series: deterministic single value
            single_key = next(iter(series_fields))
            single_val = series_fields[single_key]
            normalized[single_key] = single_val
            normalized["value"] = single_val
        elif len(series_fields) > 1:
            # Multi-series: deterministic values mapping
            normalized.update(series_fields)
            normalized["values"] = series_fields
        else:
            normalized["value"] = None

        return normalized

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        val = normalized.get("value")
        if val is not None and val < 0:
            warnings.append(f"Suspicious negative value in macroeconomic series: {val}")
        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        metadata: Dict[str, Any] = {}
        if response.provider_symbol:
            from backend.engine.private.precious_metals.registry import PreciousMetalSeriesRegistry
            pm_def = PreciousMetalSeriesRegistry.get(response.provider_symbol)
            if pm_def:
                metadata["originating_source"] = pm_def.originating_source
                metadata["metal"] = pm_def.metal.value
                metadata["currency"] = pm_def.currency.value
                metadata["quantity_unit"] = pm_def.quantity_unit.value
                metadata["price_type"] = pm_def.price_type.value

        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=self.base_url,
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=metadata,
        )

    @staticmethod
    def _extract_series_value(item: Dict[str, Any], series_code: str) -> Any:
        alt_key = series_code.replace(".", "_")
        if series_code in item:
            return item[series_code]
        if alt_key in item:
            return item[alt_key]
        return None

    @staticmethod
    def _parse_decimal(val_str: Any) -> Optional[float]:
        if val_str is None:
            return None
        cleaned = str(val_str).strip()
        if cleaned in ("", "-", "null", "None"):
            return None
        try:
            # 0 and 0.0 are valid observations
            return float(cleaned.replace(",", "."))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_exact_decimal(val_str: Any) -> Optional[Decimal]:
        if val_str is None:
            return None
        cleaned = str(val_str).strip()
        if cleaned in ("", "-", "null", "None"):
            return None
        try:
            return Decimal(cleaned.replace(",", "."))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _parse_date(date_str: Optional[str]) -> Optional[date]:
        if not date_str:
            return None
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        return None
