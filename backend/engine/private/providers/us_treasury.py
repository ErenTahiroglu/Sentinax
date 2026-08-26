"""
backend/engine/private/providers/us_treasury.py
=================================================
U.S. Department of the Treasury Daily Interest Rate XML Feed Provider.

Official Specification:
    - Base URL: https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml
    - Parameter: data=daily_treasury_yield_curve
    - Feed Format: Atom XML with Microsoft OData DataServices properties
    - Monthly Bounded Queries: field_tdr_date_value_month=YYYYMM
    - Authentication: Open public service (No API key required).

Point-in-Time & Semantic Invariants:
    - Single HTTP curve fetch serves all maturities (1M, 2M, 3M, 4M, 6M, 1Y, 2Y, 3Y, 5Y, 7Y, 10Y, 20Y, 30Y).
    - Missing maturity elements parse as None; 0.0 is preserved.
    - Historical SYSTEM_AS_OF and SOURCE_AS_OF are rejected (external PIT reconstruction unsupported).
    - Historical effective_date queries check exact date entry (no forward/backfill).
    - Provider does NOT calculate yield spreads (10Y-2Y, etc.); provides pure raw par yields.
    - Preserves 6 December 2021 methodology break (quasi-cubic Hermite spline -> monotone convex spline).
    - Raw official XML is preserved in response payload for immutable snapshotting.
    - Missing provider_symbol fails fast without silent 10Y default.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4
import xml.etree.ElementTree as ET

import httpx

from backend.engine.private.domain import (
    AsOfMode,
    DataConfidenceLevel,
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
    MacroCategory,
    MacroFrequency,
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

# Standard XML Namespaces for U.S. Treasury DataServices Feed
XML_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
}

# Mapping between canonical tenors and XML element tags
TENOR_TAG_MAP = {
    "1M": "BC_1MONTH",
    "2M": "BC_2MONTH",
    "3M": "BC_3MONTH",
    "4M": "BC_4MONTH",
    "6M": "BC_6MONTH",
    "1Y": "BC_1YEAR",
    "2Y": "BC_2YEAR",
    "3Y": "BC_3YEAR",
    "5Y": "BC_5YEAR",
    "7Y": "BC_7YEAR",
    "10Y": "BC_10YEAR",
    "20Y": "BC_20YEAR",
    "30Y": "BC_30YEAR",
}


class USTreasuryYieldCurveProvider(DataProviderContract):
    """
    Adapter for U.S. Department of the Treasury Daily Treasury Par Yield Curve XML Feed.
    """
    provider_name: str = "US_TREASURY"
    provider_version: str = "1.1.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._http_client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        return self._http_client or get_http_client()

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Fetches daily Treasury yield curve observations.
        """
        # 1. Reject missing provider symbol (No silent 10Y default)
        if not context.provider_symbol:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["No Treasury maturity or provider_symbol specified."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 2. Resolve requested tenor and series definition
        canonical_def = None
        target_field = context.provider_symbol

        if context.provider_symbol.startswith("US_TREASURY_"):
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
                target_field = canonical_def.provider_series_code

        # 3. Historical Mode Validation & Fail-Closed Guards
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
                    warnings=["U.S. Treasury historical source vintage unavailable; local PIT storage required."],
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

        # 4. Determine month parameter (Bounded query)
        now_utc = datetime.now(timezone.utc)
        if context.effective_date:
            month_param = context.effective_date.strftime("%Y%m")
            target_date = context.effective_date
        else:
            month_param = now_utc.strftime("%Y%m")
            target_date = None

        params = {
            "data": "daily_treasury_yield_curve",
            "field_tdr_date_value_month": month_param,
        }

        client = self._get_client()
        t_retrieved = datetime.now(timezone.utc)

        try:
            resp = await client.get(
                self.base_url,
                params=params,
                timeout=12.0,
                headers={"Accept": "application/xml, text/xml"},
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Treasury XML request timed out: {e}", provider_name=self.provider_name)
        except httpx.NetworkError as e:
            raise ProviderServerError(f"Treasury network error: {e}", provider_name=self.provider_name)

        if resp.status_code in (401, 403):
            raise ProviderAuthenticationError("Treasury access forbidden.", provider_name=self.provider_name)
        if resp.status_code == 429:
            raise ProviderRateLimitError("Treasury rate limit exceeded.", provider_name=self.provider_name)
        if resp.status_code >= 500:
            raise ProviderServerError(f"Treasury server error: HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)
        if resp.status_code != 200:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"Treasury returned HTTP {resp.status_code}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 5. Parse XML feed
        xml_text = resp.text
        curves_by_date = self._parse_yield_curve_xml(xml_text)

        if not curves_by_date:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw={"xml_text": xml_text, "entries": []},
                warnings=["Treasury returned 0 curve records for requested month."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 6. Select target curve entry
        selected_curve: Optional[Dict[str, Any]] = None
        selected_date: Optional[date] = None

        if target_date:
            if target_date in curves_by_date:
                selected_date = target_date
                selected_curve = curves_by_date[target_date]
            else:
                # Exact date not found -> UNAVAILABLE (no forward/backfill)
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=t_retrieved,
                    published_at=None,
                    effective_date=target_date,
                    status=DataStatus.UNAVAILABLE,
                    raw={"xml_text": xml_text, "available_dates": [d.isoformat() for d in curves_by_date.keys()]},
                    warnings=[f"Date '{target_date}' not found in Treasury feed for month '{month_param}'."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )
        else:
            # Latest available date in month
            sorted_dates = sorted(curves_by_date.keys())
            selected_date = sorted_dates[-1]
            selected_curve = curves_by_date[selected_date]

        # Extract target field value
        maturities = selected_curve.get("maturities", {})
        target_val = maturities.get(target_field)
        if target_val is None:
            # Check if tenor shorthand (e.g. "10Y") was passed
            for tenor, tag in TENOR_TAG_MAP.items():
                if target_field in (tenor, tag):
                    target_val = maturities.get(tag)
                    target_field = tag
                    break

        status = DataStatus.COMPLETE if target_val is not None else DataStatus.UNAVAILABLE

        source_meta: Dict[str, Any] = {
            "delivery_provider": "U.S. Department of the Treasury",
            "source_role": "SOVEREIGN_FISCAL_AUTHORITY",
            "origin_source": "U.S. Department of the Treasury",
            "release_name": "Daily Treasury Par Yield Curve Rates",
            "feed_type": "daily_treasury_yield_curve",
            "methodology_note": "Monotone convex spline (effective 2021-12-06); prior observations use quasi-cubic Hermite spline.",
            "maturities": maturities,
            "target_field": target_field,
            "source_available_date": None,
            "availability_precision": None,
        }

        # Raw payload carries original XML text and complete normalized data (no value loss)
        raw_payload: Dict[str, Any] = {
            "xml_text": xml_text,
            "selected_date": selected_date.isoformat(),
            "target_field": target_field,
            "value": target_val,
            "maturities": maturities,
        }

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=None,
            effective_date=selected_date,
            observed_at=t_retrieved,
            status=status,
            raw=raw_payload,
            warnings=[] if status == DataStatus.COMPLETE else [f"Maturity '{target_field}' missing for date '{selected_date}'."],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
            source_metadata=source_meta,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """
        Normalizes Treasury raw payload deterministically.
        """
        if not isinstance(raw, dict):
            return {}

        val = raw.get("value")
        target_f = raw.get("target_field")
        maturities = raw.get("maturities", {})

        if val is None and target_f and target_f in maturities:
            val = maturities[target_f]

        return {
            "date": raw.get("selected_date") or raw.get("date"),
            "target_field": target_f,
            "value": val,
            "maturities": maturities,
        }

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return []

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        meta = dict(response.source_metadata)
        meta["delivery_provider"] = "U.S. Department of the Treasury"
        meta["source_role"] = "SOVEREIGN_FISCAL_AUTHORITY"
        meta["origin_source"] = "U.S. Department of the Treasury"

        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=self.base_url,
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=meta,
        )

    @staticmethod
    def materialize_curve_observations(
        response: ProviderResponse,
        snapshot_id: Optional[UUID] = None,
    ) -> List[MacroObservationRecord]:
        """
        Fan-out helper: Materializes individual MacroObservationRecord instances for all 4 canonical
        Treasury tenors (3M, 2Y, 10Y, 30Y) from a single curve response sharing the same raw snapshot_id.
        No spread calculations are performed.
        """
        if not response.is_usable or response.effective_date is None or response.retrieved_at is None:
            return []

        maturities = response.source_metadata.get("maturities", {})
        if not maturities and isinstance(response.raw, dict):
            maturities = response.raw.get("maturities", {})

        tenor_mapping = {
            "US_TREASURY_PAR_3M": "BC_3MONTH",
            "US_TREASURY_PAR_2Y": "BC_2YEAR",
            "US_TREASURY_PAR_10Y": "BC_10YEAR",
            "US_TREASURY_PAR_30Y": "BC_30YEAR",
        }

        records: List[MacroObservationRecord] = []
        for series_key, field_tag in tenor_mapping.items():
            val = maturities.get(field_tag)
            rec = MacroObservationRecord(
                series_key=series_key,
                effective_date=response.effective_date,
                value=val,
                unit=MacroUnit.PERCENT,
                frequency=MacroFrequency.BUSINESS_DAILY,
                data_status=DataStatus.COMPLETE if val is not None else DataStatus.UNAVAILABLE,
                confidence_level=DataConfidenceLevel.HIGH if val is not None else DataConfidenceLevel.NONE,
                source_tier=SourceTier.TIER_1_REGULATORY,
                retrieved_at=response.retrieved_at,
                observed_at=response.observed_at or response.retrieved_at,
                published_at=None,
                source_available_date=None,
                availability_precision=None,
                origin_source="U.S. Department of the Treasury",
                release_name="Daily Treasury Par Yield Curve Rates",
                snapshot_id=snapshot_id,
                warnings=list(response.warnings),
                raw_payload={"field_tag": field_tag, "value": val, "curve_date": response.effective_date.isoformat()},
            )
            records.append(rec)

        return records

    async def get_all_curves_page(self, page: int = 0) -> List[Dict[str, Any]]:
        """
        Helper for historical backfill: fetches a paginated slice of all Treasury history with typed errors.
        """
        params = {
            "data": "daily_treasury_yield_curve",
            "field_tdr_date_value": "all",
            "page": page,
        }
        client = self._get_client()

        try:
            resp = await client.get(self.base_url, params=params, timeout=15.0)
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Treasury backfill request timed out: {e}", provider_name=self.provider_name)
        except httpx.NetworkError as e:
            raise ProviderServerError(f"Treasury backfill network error: {e}", provider_name=self.provider_name)

        if resp.status_code in (401, 403):
            raise ProviderAuthenticationError("Treasury backfill access forbidden.", provider_name=self.provider_name)
        if resp.status_code == 429:
            raise ProviderRateLimitError("Treasury rate limit exceeded.", provider_name=self.provider_name)
        if resp.status_code >= 500:
            raise ProviderServerError(f"Treasury server error: HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)
        if resp.status_code != 200:
            raise ProviderServerError(f"Treasury backfill returned HTTP {resp.status_code}", status_code=resp.status_code, provider_name=self.provider_name)

        curves = self._parse_yield_curve_xml(resp.text)
        return list(curves.values())

    async def get_all_curves(self, max_pages: int = 100) -> List[Dict[str, Any]]:
        """
        Historical backfill helper: traverses all pages starting at page=0 until no entries remain.
        """
        all_curves: List[Dict[str, Any]] = []
        page = 0
        while page < max_pages:
            curves_page = await self.get_all_curves_page(page=page)
            if not curves_page:
                break
            all_curves.extend(curves_page)
            page += 1
        return all_curves

    @staticmethod
    def _parse_yield_curve_xml(xml_text: str) -> Dict[date, Dict[str, Any]]:
        if not xml_text or not xml_text.strip():
            return {}

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise ProviderSchemaError(f"Failed to parse Treasury XML: {e}", provider_name="US_TREASURY")

        curves: Dict[date, Dict[str, Any]] = {}

        # Look for entry elements across namespaces
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        if not entries:
            entries = root.findall(".//entry")

        for entry in entries:
            props = entry.find(".//{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}properties")
            if props is None:
                props = entry.find(".//properties")
            if props is None:
                continue

            date_elem = props.find("{http://schemas.microsoft.com/ado/2007/08/dataservices}NEW_DATE")
            if date_elem is None:
                date_elem = props.find("NEW_DATE")
            if date_elem is None or not date_elem.text:
                continue

            # Date format: YYYY-MM-DD or YYYY-MM-DDT00:00:00
            raw_date_str = date_elem.text.strip().split("T")[0]
            try:
                obs_date = datetime.strptime(raw_date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            maturities: Dict[str, Optional[float]] = {}
            for child in props:
                tag_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag_name.startswith("BC_"):
                    val_text = child.text.strip() if child.text else None
                    if val_text in ("", "null", "None", None):
                        maturities[tag_name] = None
                    else:
                        try:
                            maturities[tag_name] = float(val_text)
                        except ValueError:
                            maturities[tag_name] = None

            curves[obs_date] = {
                "date": obs_date,
                "maturities": maturities,
            }

        return curves
