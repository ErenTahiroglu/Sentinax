"""
backend/engine/private/providers/alpha_vantage_eod.py
=====================================================
Alpha Vantage Global EOD Market Data Adapter (US & Europe Equities/ETFs).

Access Classification:
    - Provider Access: YELLOW (Commercial API, free-tier capacity constrained: 25 requests/day standard limit).
    - Source Quality: TIER_3_AGGREGATOR (commercial market data aggregator; not an official exchange authority).
    - Capabilities: LOW_VOLUME, PER_SYMBOL_REQUEST, FREE_DAILY_LIMIT_CONSTRAINED, FREE_COMPACT_HISTORY.

Hardening Invariants:
    - Zero float usage: all OHLC prices and volumes are exact Decimal; Python float inputs are strictly rejected.
    - Missing fields remain None (missing != zero).
    - Non-negative OHLC: all prices (open, high, low, close) and volume must be >= 0.
    - Non-finite values (NaN, Infinity, -Infinity) are rejected with invalid observation status.
    - Strict Point-in-Time semantics: trade_date (economic date) vs retrieved_at (network UTC).
    - published_at is None unless explicitly supplied (no fabrication).
    - Response metadata symbol validation: "2. Symbol" in Meta Data must match requested symbol.
    - Request identity binding: FetchContext with conflicting (canonical_instrument_id, provider_symbol) fails closed before network.
    - Aggregate status calculation:
        * No observations or 0 VALID observations -> UNAVAILABLE
        * All observations VALID -> COMPLETE
        * Mix of VALID and invalid/unresolved/conflict -> PARTIAL
        * Rate limited -> UNAVAILABLE
    - Symbol identity resolves through InstrumentResolverService; unmapped aliases fail closed (UNRESOLVED_IDENTITY).
    - Preserves canonical InstrumentType (US_STOCK, EUROPEAN_STOCK, US_ETF, EUROPEAN_ETF).
    - Per-instrument snapshot scope: failure or absence of one symbol does not erase other instruments.
    - Provider errors inside HTTP 200 JSON (rate limit, invalid symbol, invalid key) are detected and handled explicitly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID, uuid4

import httpx

from backend.engine.private.domain import (
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.identity import InstrumentResolverService
from backend.engine.private.market_data.global_models import (
    AlphaVantageCapability,
    GlobalEODObservation,
    GlobalEODSnapshot,
    GlobalObservationStatus,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)

ALPHA_VANTAGE_PROVIDER_NAME = "ALPHA_VANTAGE"
ALPHA_VANTAGE_PROVIDER_VERSION = "1.0.0"
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
ALPHA_VANTAGE_FUNCTION = "TIME_SERIES_DAILY"
ALPHA_VANTAGE_FREE_DAILY_LIMIT = 25
ALPHA_VANTAGE_COMPACT_LIMIT = 100


def _parse_finite_decimal(value: Any) -> Optional[Decimal]:
    """
    Parses a string or integer value into an exact finite Decimal.
    STRICT: Rejects Python float inputs to prevent loss-of-precision contamination.
    """
    if value is None:
        return None
    # Reject float inputs explicitly
    if isinstance(value, float):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        val_str = value.strip().replace(",", "")
        if not val_str or val_str.lower() in ("none", "null", "nan", "inf", "-inf", "infinity", "-infinity"):
            return None
        try:
            d = Decimal(val_str)
            if not d.is_finite():
                return None
            return d
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


def _check_non_finite_raw(value: Any) -> bool:
    """Returns True if the raw value explicitly represents a non-finite number."""
    if value is None:
        return False
    if isinstance(value, float):
        import math
        return math.isnan(value) or math.isinf(value)
    val_str = str(value).strip().lower()
    return val_str in ("nan", "snan", "infinity", "+infinity", "-infinity", "inf", "-inf")


class AlphaVantageEODProvider(DataProviderContract):
    """
    Data provider adapter for Alpha Vantage global equity and ETF daily EOD time series.
    """
    provider_name: str = ALPHA_VANTAGE_PROVIDER_NAME
    provider_version: str = ALPHA_VANTAGE_PROVIDER_VERSION
    source_quality: SourceTier = SourceTier.TIER_3_AGGREGATOR
    access_status: ProviderAccessStatus = ProviderAccessStatus.YELLOW

    # Access Classification & Metadata Flags
    official_source: bool = False
    developer_api: bool = True
    sla_guaranteed: bool = False

    capabilities: List[AlphaVantageCapability] = [
        AlphaVantageCapability.LOW_VOLUME,
        AlphaVantageCapability.PER_SYMBOL_REQUEST,
        AlphaVantageCapability.FREE_DAILY_LIMIT_CONSTRAINED,
        AlphaVantageCapability.FREE_COMPACT_HISTORY,
    ]

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY") or os.getenv("ALPHAVANTAGE_API_KEY")

    # ─────────────────────────────────────────────────────────────────────────
    # Parsing & Normalization
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def parse_daily_series(
        cls,
        raw_json: Any,
        provider_symbol: str,
        retrieved_at: datetime,
        http_status: int = 200,
        resolver: Optional[InstrumentResolverService] = None,
        snapshot_id: Optional[UUID] = None,
    ) -> GlobalEODSnapshot:
        """
        Parses raw Alpha Vantage TIME_SERIES_DAILY response into a GlobalEODSnapshot
        and normalized GlobalEODObservation instances.
        """
        snap_id = snapshot_id or uuid4()
        clean_symbol = provider_symbol.strip().upper()

        if isinstance(raw_json, str):
            raw_str = raw_json
            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError as err:
                payload_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()
                return GlobalEODSnapshot(
                    id=snap_id,
                    provider=ALPHA_VANTAGE_PROVIDER_NAME,
                    provider_symbol=clean_symbol,
                    retrieved_at=retrieved_at,
                    http_status=http_status,
                    payload_hash=payload_hash,
                    raw_payload=raw_str,
                    diagnostics=[f"MALFORMED_JSON: Failed to decode Alpha Vantage response: {err}"],
                )
        else:
            data = raw_json
            raw_str = json.dumps(raw_json, sort_keys=True, ensure_ascii=False, default=str)

        payload_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()
        diagnostics: List[str] = []
        is_rate_limited = False

        if not isinstance(data, dict):
            return GlobalEODSnapshot(
                id=snap_id,
                provider=ALPHA_VANTAGE_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_str,
                diagnostics=["INVALID_ROOT: Expected JSON object at root of response."],
            )

        # 1. Detect Provider Error Messages & Rate Limits embedded in HTTP 200 JSON
        if "Information" in data or "Note" in data:
            info_msg = str(data.get("Information") or data.get("Note"))
            lower_msg = info_msg.lower()
            if "rate limit" in lower_msg or "requests per day" in lower_msg or "call frequency" in lower_msg:
                is_rate_limited = True
                diagnostics.append(f"RATE_LIMIT_EXHAUSTED: Alpha Vantage rate limit response received: {info_msg}")
            else:
                diagnostics.append(f"PROVIDER_INFO: {info_msg}")

        if "Error Message" in data:
            err_msg = str(data["Error Message"])
            diagnostics.append(f"PROVIDER_ERROR: {err_msg}")

        # 2. Extract Time Series Dictionary
        time_series = data.get("Time Series (Daily)")
        if not time_series or not isinstance(time_series, dict):
            if not diagnostics:
                diagnostics.append("EMPTY_SERIES: No 'Time Series (Daily)' object present in response.")
            return GlobalEODSnapshot(
                id=snap_id,
                provider=ALPHA_VANTAGE_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_str,
                is_rate_limited=is_rate_limited,
                diagnostics=diagnostics,
            )

        # 3. Response Metadata Symbol Validation
        output_size = "compact"
        meta_data = data.get("Meta Data")
        if not meta_data or not isinstance(meta_data, dict) or "2. Symbol" not in meta_data:
            diagnostics.append("INVALID_SOURCE_CONTEXT: Response contains time series but lacks authoritative '2. Symbol' in Meta Data.")
            return GlobalEODSnapshot(
                id=snap_id,
                provider=ALPHA_VANTAGE_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_str,
                is_rate_limited=is_rate_limited,
                diagnostics=diagnostics,
            )

        resp_symbol = str(meta_data["2. Symbol"]).strip().upper()
        output_size = str(meta_data.get("4. Output Size", "compact")).lower()

        if resp_symbol != clean_symbol:
            diagnostics.append(f"RESPONSE_SYMBOL_MISMATCH: Requested symbol '{clean_symbol}', but response metadata returned symbol '{resp_symbol}'.")
            return GlobalEODSnapshot(
                id=snap_id,
                provider=ALPHA_VANTAGE_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_str,
                output_size=output_size,
                is_rate_limited=is_rate_limited,
                diagnostics=diagnostics,
            )

        # 4. Parse Daily Observation Rows
        parsed_observations: List[GlobalEODObservation] = []
        raw_rows_by_date: Dict[date, List[Dict[str, Any]]] = {}

        for date_str, row_dict in time_series.items():
            if not isinstance(row_dict, dict):
                continue
            try:
                t_date = date.fromisoformat(str(date_str).strip())
            except ValueError:
                diagnostics.append(f"MALFORMED_DATE: Invalid ISO date format '{date_str}'.")
                continue

            raw_rows_by_date.setdefault(t_date, []).append(row_dict)

        for t_date, rows in raw_rows_by_date.items():
            for row_dict in rows:
                row_diags: List[str] = []
                status = GlobalObservationStatus.VALID

                # Check for explicit non-finite values in raw fields
                has_non_finite = any(
                    _check_non_finite_raw(row_dict.get(k))
                    for k in ("1. open", "2. high", "3. low", "4. close", "5. volume")
                )
                if has_non_finite:
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append("NON_FINITE_DECIMAL: Non-finite value (NaN/Infinity) encountered in price or volume.")

                open_val = _parse_finite_decimal(row_dict.get("1. open"))
                high_val = _parse_finite_decimal(row_dict.get("2. high"))
                low_val = _parse_finite_decimal(row_dict.get("3. low"))
                close_val = _parse_finite_decimal(row_dict.get("4. close"))
                volume_val = _parse_finite_decimal(row_dict.get("5. volume"))

                # Validate Non-Negative OHLC Prices
                if open_val is not None and open_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_PRICE: Negative open price {open_val}.")
                if high_val is not None and high_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_PRICE: Negative high price {high_val}.")
                if low_val is not None and low_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_PRICE: Negative low price {low_val}.")
                if close_val is not None and close_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_PRICE: Negative close price {close_val}.")

                # Validate Close Price Presence
                if close_val is None:
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append("MISSING_CLOSE: Close price is missing, null, or non-finite.")

                # Validate OHLC Envelope
                if high_val is not None and low_val is not None:
                    if high_val < low_val:
                        status = GlobalObservationStatus.INVALID_OBSERVATION
                        row_diags.append(f"OHLC_ENVELOPE_VIOLATION: High {high_val} is less than Low {low_val}.")
                    if open_val is not None:
                        if high_val < open_val or low_val > open_val:
                            status = GlobalObservationStatus.INVALID_OBSERVATION
                            row_diags.append(f"OHLC_ENVELOPE_VIOLATION: Open {open_val} outside High/Low range [{low_val}, {high_val}].")
                    if close_val is not None:
                        if high_val < close_val or low_val > close_val:
                            status = GlobalObservationStatus.INVALID_OBSERVATION
                            row_diags.append(f"OHLC_ENVELOPE_VIOLATION: Close {close_val} outside High/Low range [{low_val}, {high_val}].")

                # Validate Volume
                if volume_val is not None and volume_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_VOLUME: Negative volume {volume_val}.")

                # Instrument Master Resolution
                instrument_id: Optional[UUID] = None
                instrument_type: Optional[InstrumentType] = None
                currency: Optional[Currency] = None
                exchange_mic: Optional[str] = None

                if resolver:
                    instrument_id = resolver.resolve_provider_symbol_to_instrument_id(
                        ALPHA_VANTAGE_PROVIDER_NAME, clean_symbol, as_of_date=t_date
                    )
                    if instrument_id:
                        inst = resolver.get_instrument_by_id(instrument_id)
                        if inst:
                            instrument_type = inst.instrument_type
                            currency = inst.currency
                            exchange_mic = inst.mic
                    else:
                        if status == GlobalObservationStatus.VALID:
                            status = GlobalObservationStatus.UNRESOLVED_IDENTITY
                        row_diags.append(f"UNRESOLVED_IDENTITY: No master instrument mapped for alias ALPHA_VANTAGE:{clean_symbol} on {t_date.isoformat()}.")
                else:
                    if status == GlobalObservationStatus.VALID:
                        status = GlobalObservationStatus.UNRESOLVED_IDENTITY
                    row_diags.append(f"UNRESOLVED_IDENTITY: No InstrumentResolverService provided for alias ALPHA_VANTAGE:{clean_symbol}.")

                obs = GlobalEODObservation(
                    provider_symbol=clean_symbol,
                    trade_date=t_date,
                    close=close_val,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    volume=volume_val,
                    currency=currency,
                    exchange=exchange_mic,
                    instrument_id=instrument_id,
                    instrument_type=instrument_type,
                    provider=ALPHA_VANTAGE_PROVIDER_NAME,
                    snapshot_id=snap_id,
                    payload_hash=payload_hash,
                    retrieved_at=retrieved_at,
                    published_at=None,  # Not supplied by Alpha Vantage
                    status=status,
                    confidence_level=DataConfidenceLevel.MEDIUM if status == GlobalObservationStatus.VALID else DataConfidenceLevel.NONE,
                    diagnostics=row_diags,
                )
                parsed_observations.append(obs)

        # 5. Handle Duplicate Rows within Snapshot
        grouped_by_date: Dict[date, List[GlobalEODObservation]] = {}
        for obs in parsed_observations:
            grouped_by_date.setdefault(obs.trade_date, []).append(obs)

        final_observations: List[GlobalEODObservation] = []
        for t_date, obs_list in grouped_by_date.items():
            if len(obs_list) == 1:
                final_observations.append(obs_list[0])
            else:
                # Check for identical logical fingerprint
                fingerprints = {
                    (o.instrument_id, o.trade_date, o.provider_symbol, o.open, o.high, o.low, o.close, o.volume, o.currency, o.status)
                    for o in obs_list
                }
                if len(fingerprints) == 1:
                    # Exact duplicate -> choose deterministic representative
                    selected = min(obs_list, key=lambda o: str(o.id))
                    final_observations.append(selected)
                else:
                    # Conflict between differing rows for same date
                    for o in obs_list:
                        o.status = GlobalObservationStatus.DUPLICATE_CONFLICT
                        o.confidence_level = DataConfidenceLevel.NONE
                        o.diagnostics.append("DUPLICATE_CONFLICT: Differing observation rows encountered for identical trade date.")
                        final_observations.append(o)

        # Sort chronologically by trade_date
        final_observations.sort(key=lambda o: (o.trade_date, str(o.id)))

        min_date = final_observations[0].trade_date if final_observations else None
        max_date = final_observations[-1].trade_date if final_observations else None

        return GlobalEODSnapshot(
            id=snap_id,
            provider=ALPHA_VANTAGE_PROVIDER_NAME,
            provider_symbol=clean_symbol,
            retrieved_at=retrieved_at,
            http_status=http_status,
            payload_hash=payload_hash,
            raw_payload=raw_str,
            output_size=output_size,
            is_rate_limited=is_rate_limited,
            trade_date_range=(min_date, max_date),
            observations=final_observations,
            diagnostics=diagnostics,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Async Fetch Implementation (DataProviderContract)
    # ─────────────────────────────────────────────────────────────────────────

    async def fetch(
        self,
        context: FetchContext,
        resolver: Optional[InstrumentResolverService] = None,
    ) -> ProviderResponse:
        """
        Retrieves daily EOD data asynchronously for a provider_symbol or canonical instrument.
        """
        retrieved_at = datetime.now(timezone.utc)
        provider_symbol = context.provider_symbol
        canonical_id = context.canonical_instrument_id

        # 1. Request Identity Binding Verification
        if canonical_id and provider_symbol and resolver:
            resolved_id = resolver.resolve_provider_symbol_to_instrument_id(
                ALPHA_VANTAGE_PROVIDER_NAME, provider_symbol, as_of_date=context.effective_date
            )
            if resolved_id != canonical_id:
                # Conflicting dual identity in FetchContext -> fail closed before network call!
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[
                        f"IDENTITY_MISMATCH: FetchContext provider_symbol '{provider_symbol}' resolves to {resolved_id}, "
                        f"which mismatches canonical_instrument_id '{canonical_id}'."
                    ],
                    canonical_instrument_id=None,
                    provider_symbol=provider_symbol,
                )

        # 2. Resolve provider_symbol if only canonical_instrument_id provided
        if not provider_symbol and canonical_id and resolver:
            provider_symbol = resolver.resolve_instrument_id_to_provider_symbol(
                canonical_id, ALPHA_VANTAGE_PROVIDER_NAME, as_of_date=context.effective_date
            )

        if not provider_symbol:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["UNRESOLVED_SYMBOL: No provider_symbol supplied or resolved for Alpha Vantage request."],
                canonical_instrument_id=canonical_id,
                provider_symbol=None,
            )

        clean_symbol = provider_symbol.strip().upper()

        # Validate canonical ID if only symbol was given
        if not canonical_id and resolver:
            canonical_id = resolver.resolve_provider_symbol_to_instrument_id(
                ALPHA_VANTAGE_PROVIDER_NAME, clean_symbol, as_of_date=context.effective_date
            )

        api_key = self._api_key or os.getenv("ALPHA_VANTAGE_API_KEY") or os.getenv("ALPHAVANTAGE_API_KEY")

        if not api_key:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["AUTH_ERROR: ALPHA_VANTAGE_API_KEY is not configured in environment or provider instance."],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        output_size = context.request_parameters.get("outputsize", "compact")
        params = {
            "function": ALPHA_VANTAGE_FUNCTION,
            "symbol": clean_symbol,
            "outputsize": output_size,
            "apikey": api_key,
        }

        try:
            client = get_http_client()
            resp = await client.get(ALPHA_VANTAGE_BASE_URL, params=params)
            retrieved_at = datetime.now(timezone.utc)
            http_status = resp.status_code

            if http_status == 429:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=["RATE_LIMITED: HTTP 429 Too Many Requests from Alpha Vantage."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

            if http_status >= 500:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"SERVER_ERROR: HTTP {http_status} server error from Alpha Vantage."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

            raw_text = resp.text
            snapshot = self.parse_daily_series(
                raw_text, clean_symbol, retrieved_at, http_status=http_status, resolver=resolver
            )

            # 3. Compute Aggregate Observation Counts and Status
            obs_count = len(snapshot.observations)
            valid_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.VALID)
            invalid_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.INVALID_OBSERVATION)
            unresolved_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.UNRESOLVED_IDENTITY)
            conflict_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.DUPLICATE_CONFLICT)

            if snapshot.is_rate_limited:
                status = DataStatus.UNAVAILABLE
            elif obs_count == 0:
                status = DataStatus.UNAVAILABLE
            elif valid_count == 0:
                status = DataStatus.UNAVAILABLE
            elif valid_count == obs_count:
                status = DataStatus.COMPLETE
            else:
                status = DataStatus.PARTIAL

            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=status,
                raw=snapshot,
                warnings=list(snapshot.diagnostics),
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
                source_metadata={
                    "payload_hash": snapshot.payload_hash,
                    "is_rate_limited": snapshot.is_rate_limited,
                    "output_size": snapshot.output_size,
                    "observation_count": obs_count,
                    "valid_count": valid_count,
                    "invalid_count": invalid_count,
                    "unresolved_count": unresolved_count,
                    "conflict_count": conflict_count,
                },
            )

        except (httpx.TimeoutException, asyncio.TimeoutError) as err:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"TIMEOUT: Alpha Vantage request timed out: {err}"],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )
        except Exception as err:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"NETWORK_ERROR: Network or connection error: {err}"],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """Maps raw snapshot or observation to a canonical field dict."""
        if isinstance(raw, GlobalEODSnapshot):
            return raw.to_dict()
        if isinstance(raw, GlobalEODObservation):
            return raw.to_dict()
        if isinstance(raw, dict):
            return raw
        return {"raw": str(raw)}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        """Returns warnings for anomalies or missing fields."""
        warnings: List[str] = []
        if "close" in normalized and normalized["close"] is None:
            warnings.append("Missing close price.")
        if normalized.get("status") == GlobalObservationStatus.UNRESOLVED_IDENTITY.value:
            warnings.append("Unresolved instrument identity.")
        if normalized.get("status") == GlobalObservationStatus.INVALID_OBSERVATION.value:
            warnings.append("Invalid observation.")
        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        """Returns provenance audit trail for a ProviderResponse."""
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=ALPHA_VANTAGE_FUNCTION,
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=response.source_metadata,
        )
