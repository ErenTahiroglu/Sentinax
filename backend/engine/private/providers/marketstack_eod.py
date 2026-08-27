"""
backend/engine/private/providers/marketstack_eod.py
==================================================
Marketstack Free Tier European Equities & ETFs Rolling EOD & Corporate Actions Adapter.

Access Classification:
    - Provider Access: YELLOW (Commercial API, Free Tier: 100 requests/month, 1 Year History, HTTPS).
    - Source Quality: TIER_3_AGGREGATOR (commercial market data aggregator; not an official exchange authority).
    - Capabilities: FREE_TIER, ROLLING_1Y_HISTORY, SPLITS_AND_DIVIDENDS, EOD_PRICES.

Role:
    - EU_ROLLING_1Y_HISTORY: Rolling recent 1-year EOD price series for European stocks and ETFs.
    - EU_RECENT_ADJUSTED_SERIES: Provider-adjusted OHLCV for recent risk models.
    - EU_CORPORATE_ACTION_MONITOR: Monitors splits and dividends to signal history refresh.
    - NOTE: Daily current incremental valuation remains Alpha Vantage Free.

Hardening Invariants:
    - European scope only: strictly supports InstrumentType.EUROPEAN_STOCK and InstrumentType.EUROPEAN_ETF.
    - Zero float usage: all financial prices, adjusted values, volumes, splits, and dividends are exact Decimal.
    - Missing fields remain None (missing != zero).
    - All OHLC prices non-negative: open, high, low, close, adj_open, adj_high, adj_low, adj_close >= 0.
    - Non-finite values (NaN, Infinity, -Infinity) rejected as invalid observations.
    - Strict Point-in-Time semantics: trade_date (economic date) vs retrieved_at (network UTC); published_at is None.
    - Corporate actions: captures split_factor and dividend. Sets history_refresh_required=True when split_factor != 1 or dividend > 0.
    - Pagination policy: requests limit=1000, sort=ASC. If total > returned rows, flags TRUNCATED_RESPONSE and degrades from COMPLETE.
    - Free tier history window: requested date range > 366 days rejected before HTTP with FREE_HISTORY_WINDOW_EXCEEDED.
    - Exchange & Symbol validation: response row symbol and exchange must match requested alias and canonical Instrument Master MIC.
    - Dual identity preflight: conflicting (canonical_instrument_id, provider_symbol) in FetchContext fails before HTTP.
    - Token security: MARKETSTACK_ACCESS_KEY sent via query param only; never logged, serialized, or stored in snapshots.
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
from backend.engine.private.identity import InstrumentResolverService
from backend.engine.private.market_data.global_models import (
    GlobalEODObservation,
    GlobalEODSnapshot,
    GlobalObservationStatus,
    MarketstackCapability,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)

MARKETSTACK_PROVIDER_NAME = "MARKETSTACK"
MARKETSTACK_PROVIDER_VERSION = "1.0.0"
MARKETSTACK_BASE_URL = "https://api.marketstack.com/v2/eod"


def _parse_finite_decimal(value: Any) -> Optional[Decimal]:
    """
    Parses a string or integer value into an exact finite Decimal.
    STRICT: Rejects Python float inputs to prevent loss-of-precision contamination.
    """
    if value is None:
        return None
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


def _parse_marketstack_request_date(value: Any, field_name: str) -> Tuple[Optional[date], Optional[str]]:
    """
    Parses and validates a caller-supplied date parameter for Marketstack EOD requests.

    Accepts:
        - datetime.date / datetime.datetime instances
        - Canonical ISO format: YYYY-MM-DD

    Returns:
        (parsed_date, None) if valid.
        (None, error_diagnostic) if malformed or invalid calendar date.
    """
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value.date(), None
    if isinstance(value, date):
        return value, None
    if not isinstance(value, str):
        return None, f"INVALID_DATE_PARAMETER: Field '{field_name}' must be a date object or string, got {type(value).__name__}."

    s = value.strip()
    if not s:
        return None, f"INVALID_DATE_PARAMETER: Field '{field_name}' cannot be an empty string."

    parts = s.split("-")
    if len(parts) != 3:
        return None, f"INVALID_DATE_PARAMETER: Field '{field_name}' value '{s}' must be in canonical YYYY-MM-DD format."

    y_str, m_str, d_str = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not (y_str.isdigit() and m_str.isdigit() and d_str.isdigit()):
        return None, f"INVALID_DATE_PARAMETER: Field '{field_name}' value '{s}' contains non-digit date parts."

    if len(y_str) != 4 or len(m_str) != 2 or len(d_str) != 2:
        return None, f"INVALID_DATE_PARAMETER: Field '{field_name}' must be canonical YYYY-MM-DD, got '{s}'."

    try:
        y, m, d = int(y_str), int(m_str), int(d_str)
        res = date(y, m, d)
        return res, None
    except ValueError as err:
        return None, f"INVALID_DATE_PARAMETER: Field '{field_name}' has invalid calendar date '{s}': {err}."


class MarketstackEODProvider(DataProviderContract):
    """
    Data provider adapter for Marketstack European Equities & ETFs Rolling EOD & Corporate Actions.
    """
    provider_name: str = MARKETSTACK_PROVIDER_NAME
    provider_version: str = MARKETSTACK_PROVIDER_VERSION
    source_quality: SourceTier = SourceTier.TIER_3_AGGREGATOR
    access_status: ProviderAccessStatus = ProviderAccessStatus.YELLOW

    official_source: bool = False
    developer_api: bool = True
    sla_guaranteed: bool = False

    capabilities: List[MarketstackCapability] = [
        MarketstackCapability.FREE_TIER,
        MarketstackCapability.ROLLING_1Y_HISTORY,
        MarketstackCapability.SPLITS_AND_DIVIDENDS,
        MarketstackCapability.EOD_PRICES,
    ]

    def __init__(self, access_key: Optional[str] = None) -> None:
        self._access_key = access_key or os.getenv("MARKETSTACK_ACCESS_KEY") or os.getenv("MARKETSTACK_KEY")

    # ─────────────────────────────────────────────────────────────────────────
    # Parsing & Normalization
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def parse_eod_response(
        cls,
        raw_json: Any,
        provider_symbol: str,
        retrieved_at: datetime,
        http_status: int = 200,
        resolver: Optional[InstrumentResolverService] = None,
        snapshot_id: Optional[UUID] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        expected_mic: Optional[str] = None,
    ) -> GlobalEODSnapshot:
        """
        Parses raw Marketstack daily EOD JSON response into a GlobalEODSnapshot
        and normalized GlobalEODObservation instances.
        """
        snap_id = snapshot_id or uuid4()
        clean_symbol = provider_symbol.strip().upper()

        if isinstance(raw_json, str):
            raw_str = raw_json
            try:
                data_obj = json.loads(raw_json, parse_float=Decimal)
            except json.JSONDecodeError as err:
                payload_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()
                return GlobalEODSnapshot(
                    id=snap_id,
                    provider=MARKETSTACK_PROVIDER_NAME,
                    provider_symbol=clean_symbol,
                    retrieved_at=retrieved_at,
                    http_status=http_status,
                    payload_hash=payload_hash,
                    raw_payload=raw_str,
                    endpoint="EOD",
                    start_date=date_from,
                    end_date=date_to,
                    output_size=None,
                    diagnostics=[f"MALFORMED_JSON: Failed to decode Marketstack response: {err}"],
                )
        else:
            data_obj = raw_json
            raw_str = json.dumps(raw_json, sort_keys=True, ensure_ascii=False, default=str)

        payload_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()
        diagnostics: List[str] = []
        is_rate_limited = False
        history_refresh_required = False

        if not isinstance(data_obj, dict):
            diagnostics.append("INVALID_ROOT: Expected JSON object at root of Marketstack response.")
            return GlobalEODSnapshot(
                id=snap_id,
                provider=MARKETSTACK_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_str,
                endpoint="EOD",
                start_date=date_from,
                end_date=date_to,
                output_size=None,
                is_rate_limited=is_rate_limited,
                diagnostics=diagnostics,
            )

        # Check for provider error in response body
        if "error" in data_obj:
            err_dict = data_obj["error"] if isinstance(data_obj["error"], dict) else {}
            err_code = str(err_dict.get("code", "unknown_error"))
            err_msg = str(err_dict.get("message", "Marketstack API error"))
            if err_code in ("usage_limit_reached", "rate_limit_reached") or "limit" in err_msg.lower():
                is_rate_limited = True
                diagnostics.append(f"RATE_LIMIT_EXHAUSTED: {err_code} - {err_msg}")
            elif err_code in ("invalid_access_key", "missing_access_key"):
                diagnostics.append(f"AUTH_ERROR: {err_code} - {err_msg}")
            else:
                diagnostics.append(f"PROVIDER_ERROR: {err_code} - {err_msg}")

            return GlobalEODSnapshot(
                id=snap_id,
                provider=MARKETSTACK_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_str,
                endpoint="EOD",
                start_date=date_from,
                end_date=date_to,
                output_size=None,
                is_rate_limited=is_rate_limited,
                diagnostics=diagnostics,
            )

        # Check pagination
        pagination = data_obj.get("pagination")
        data_list = data_obj.get("data")

        if not isinstance(pagination, dict) or not isinstance(data_list, list):
            diagnostics.append("MALFORMED_STRUCTURE: Marketstack response missing pagination or data array.")
            return GlobalEODSnapshot(
                id=snap_id,
                provider=MARKETSTACK_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_str,
                endpoint="EOD",
                start_date=date_from,
                end_date=date_to,
                output_size=None,
                is_rate_limited=is_rate_limited,
                diagnostics=diagnostics,
            )

        # Validate pagination integer fields
        p_limit = pagination.get("limit")
        p_offset = pagination.get("offset")
        p_count = pagination.get("count")
        p_total = pagination.get("total")

        if (
            (isinstance(p_limit, int) and p_limit <= 0)
            or (isinstance(p_offset, int) and p_offset < 0)
            or (isinstance(p_count, int) and p_count < 0)
            or (isinstance(p_total, int) and p_total < 0)
        ):
            diagnostics.append(
                f"INVALID_PAGINATION: Non-positive limit or negative pagination values encountered: "
                f"limit={p_limit}, offset={p_offset}, count={p_count}, total={p_total}."
            )

        returned_count = len(data_list)
        if isinstance(p_count, int) and p_count != returned_count:
            diagnostics.append(
                f"INVALID_PAGINATION: Pagination count ({p_count}) does not match returned row count ({returned_count})."
            )

        if isinstance(p_total, int) and (p_total > returned_count or (isinstance(p_count, int) and p_total > p_count)):
            diagnostics.append(
                f"TRUNCATED_RESPONSE: Marketstack pagination total ({p_total}) exceeds returned rows ({returned_count})."
            )

        parsed_observations: List[GlobalEODObservation] = []
        raw_rows_by_date: Dict[date, List[Dict[str, Any]]] = {}

        for item in data_list:
            if not isinstance(item, dict):
                continue
            date_str = item.get("date")
            if not date_str:
                continue
            try:
                t_date_str = str(date_str).split("T")[0].strip()
                t_date = date.fromisoformat(t_date_str)
            except ValueError:
                diagnostics.append(f"MALFORMED_DATE: Invalid ISO date format '{date_str}'.")
                continue

            raw_rows_by_date.setdefault(t_date, []).append(item)

        for t_date, rows in raw_rows_by_date.items():
            for row_dict in rows:
                row_diags: List[str] = []
                status = GlobalObservationStatus.VALID

                # Check for explicit non-finite values in raw fields
                has_non_finite = any(
                    _check_non_finite_raw(row_dict.get(k))
                    for k in (
                        "open", "high", "low", "close", "volume",
                        "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
                        "split_factor", "dividend"
                    )
                )
                if has_non_finite:
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append("NON_FINITE_DECIMAL: Non-finite value (NaN/Infinity) encountered in price or volume.")

                # Verify Response Symbol Matches Requested Alias
                row_symbol = str(row_dict.get("symbol", "") or "").strip().upper()
                if row_symbol != clean_symbol:
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(
                        f"INVALID_SOURCE_CONTEXT: Row symbol '{row_symbol}' does not match requested symbol '{clean_symbol}'."
                    )

                # Verify Response Exchange Matches Canonical Instrument MIC Exactly
                row_exchange = str(row_dict.get("exchange", "") or "").strip().upper()
                if expected_mic:
                    norm_mic = expected_mic.strip().upper()
                    if not row_exchange or row_exchange != norm_mic:
                        status = GlobalObservationStatus.INVALID_OBSERVATION
                        row_diags.append(
                            f"INVALID_SOURCE_CONTEXT: Row exchange '{row_exchange}' does not match canonical MIC '{norm_mic}'."
                        )

                open_val = _parse_finite_decimal(row_dict.get("open"))
                high_val = _parse_finite_decimal(row_dict.get("high"))
                low_val = _parse_finite_decimal(row_dict.get("low"))
                close_val = _parse_finite_decimal(row_dict.get("close"))
                volume_val = _parse_finite_decimal(row_dict.get("volume"))

                adj_open_val = _parse_finite_decimal(row_dict.get("adj_open"))
                adj_high_val = _parse_finite_decimal(row_dict.get("adj_high"))
                adj_low_val = _parse_finite_decimal(row_dict.get("adj_low"))
                adj_close_val = _parse_finite_decimal(row_dict.get("adj_close"))
                adj_volume_val = _parse_finite_decimal(row_dict.get("adj_volume"))

                dividend_val = _parse_finite_decimal(row_dict.get("dividend"))
                split_factor_val = _parse_finite_decimal(row_dict.get("split_factor"))

                # Corporate Action Signal
                if split_factor_val is not None and split_factor_val != Decimal("1"):
                    history_refresh_required = True
                if dividend_val is not None and dividend_val > Decimal("0"):
                    history_refresh_required = True

                # Validate Raw OHLC Prices
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

                # Validate Raw OHLC Envelope
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

                # Validate Adjusted OHLC Prices
                if adj_open_val is not None and adj_open_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_ADJUSTED_PRICE: Negative adjusted open price {adj_open_val}.")
                if adj_high_val is not None and adj_high_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_ADJUSTED_PRICE: Negative adjusted high price {adj_high_val}.")
                if adj_low_val is not None and adj_low_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_ADJUSTED_PRICE: Negative adjusted low price {adj_low_val}.")
                if adj_close_val is not None and adj_close_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_ADJUSTED_PRICE: Negative adjusted close price {adj_close_val}.")

                # Validate Adjusted OHLC Envelope
                if adj_high_val is not None and adj_low_val is not None:
                    if adj_high_val < adj_low_val:
                        status = GlobalObservationStatus.INVALID_OBSERVATION
                        row_diags.append(f"ADJUSTED_OHLC_ENVELOPE_VIOLATION: Adj High {adj_high_val} is less than Adj Low {adj_low_val}.")
                    if adj_open_val is not None:
                        if adj_high_val < adj_open_val or adj_low_val > adj_open_val:
                            status = GlobalObservationStatus.INVALID_OBSERVATION
                            row_diags.append(f"ADJUSTED_OHLC_ENVELOPE_VIOLATION: Adj Open {adj_open_val} outside Adj High/Low range [{adj_low_val}, {adj_high_val}].")
                    if adj_close_val is not None:
                        if adj_high_val < adj_close_val or adj_low_val > adj_close_val:
                            status = GlobalObservationStatus.INVALID_OBSERVATION
                            row_diags.append(f"ADJUSTED_OHLC_ENVELOPE_VIOLATION: Adj Close {adj_close_val} outside Adj High/Low range [{adj_low_val}, {adj_high_val}].")

                # Validate Volume
                if volume_val is not None and volume_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_VOLUME: Negative volume {volume_val}.")

                # Validate Split Factor & Dividend
                if split_factor_val is not None and split_factor_val <= Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"INVALID_SPLIT_FACTOR: Split factor must be positive: {split_factor_val}.")
                if dividend_val is not None and dividend_val < Decimal("0"):
                    status = GlobalObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"NEGATIVE_DIVIDEND: Dividend must be non-negative: {dividend_val}.")

                # Instrument Master Resolution
                instrument_id: Optional[UUID] = None
                instrument_type: Optional[InstrumentType] = None
                currency: Optional[Currency] = None
                exchange_mic: Optional[str] = None

                if resolver:
                    instrument_id = resolver.resolve_provider_symbol_to_instrument_id(
                        MARKETSTACK_PROVIDER_NAME, clean_symbol, as_of_date=t_date
                    )
                    if instrument_id:
                        inst = resolver.get_instrument_by_id(instrument_id)
                        if inst:
                            instrument_type = inst.instrument_type
                            currency = inst.currency
                            exchange_mic = inst.mic

                            # Check European instrument type restriction
                            if instrument_type not in (InstrumentType.EUROPEAN_STOCK, InstrumentType.EUROPEAN_ETF):
                                status = GlobalObservationStatus.INVALID_OBSERVATION
                                row_diags.append(
                                    f"UNSUPPORTED_INSTRUMENT_TYPE: Marketstack adapter only supports EUROPEAN_STOCK and EUROPEAN_ETF, got {instrument_type}."
                                )
                    else:
                        if status == GlobalObservationStatus.VALID:
                            status = GlobalObservationStatus.UNRESOLVED_IDENTITY
                        row_diags.append(f"UNRESOLVED_IDENTITY: No master instrument mapped for alias MARKETSTACK:{clean_symbol} on {t_date.isoformat()}.")
                else:
                    if status == GlobalObservationStatus.VALID:
                        status = GlobalObservationStatus.UNRESOLVED_IDENTITY
                    row_diags.append(f"UNRESOLVED_IDENTITY: No InstrumentResolverService provided for alias MARKETSTACK:{clean_symbol}.")

                obs = GlobalEODObservation(
                    provider_symbol=clean_symbol,
                    trade_date=t_date,
                    close=close_val,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    volume=volume_val,
                    adj_open=adj_open_val,
                    adj_high=adj_high_val,
                    adj_low=adj_low_val,
                    adj_close=adj_close_val,
                    adj_volume=adj_volume_val,
                    div_cash=dividend_val,
                    split_factor=split_factor_val,
                    currency=currency,
                    exchange=exchange_mic or row_exchange or None,
                    instrument_id=instrument_id,
                    instrument_type=instrument_type,
                    provider=MARKETSTACK_PROVIDER_NAME,
                    snapshot_id=snap_id,
                    payload_hash=payload_hash,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    status=status,
                    confidence_level=DataConfidenceLevel.MEDIUM if status == GlobalObservationStatus.VALID else DataConfidenceLevel.NONE,
                    diagnostics=row_diags,
                )
                parsed_observations.append(obs)

        # Handle Duplicate Rows within Snapshot
        grouped_by_date: Dict[date, List[GlobalEODObservation]] = {}
        for obs in parsed_observations:
            grouped_by_date.setdefault(obs.trade_date, []).append(obs)

        final_observations: List[GlobalEODObservation] = []
        for t_date, obs_list in grouped_by_date.items():
            if len(obs_list) == 1:
                final_observations.append(obs_list[0])
            else:
                fingerprints = {
                    (
                        o.instrument_id, o.trade_date, o.provider_symbol,
                        o.open, o.high, o.low, o.close, o.volume,
                        o.adj_open, o.adj_high, o.adj_low, o.adj_close, o.adj_volume,
                        o.div_cash, o.split_factor, o.currency, o.status
                    )
                    for o in obs_list
                }
                if len(fingerprints) == 1:
                    selected = min(obs_list, key=lambda o: str(o.id))
                    final_observations.append(selected)
                else:
                    for o in obs_list:
                        o.status = GlobalObservationStatus.DUPLICATE_CONFLICT
                        o.confidence_level = DataConfidenceLevel.NONE
                        o.diagnostics.append("DUPLICATE_CONFLICT: Differing observation rows encountered for identical trade date.")
                        final_observations.append(o)

        final_observations.sort(key=lambda o: (o.trade_date, str(o.id)))

        min_date = final_observations[0].trade_date if final_observations else None
        max_date = final_observations[-1].trade_date if final_observations else None

        snap_inst_id: Optional[UUID] = None
        if resolver:
            snap_inst_id = resolver.resolve_provider_symbol_to_instrument_id(
                MARKETSTACK_PROVIDER_NAME, clean_symbol
            )

        return GlobalEODSnapshot(
            id=snap_id,
            provider=MARKETSTACK_PROVIDER_NAME,
            provider_symbol=clean_symbol,
            instrument_id=snap_inst_id,
            retrieved_at=retrieved_at,
            http_status=http_status,
            payload_hash=payload_hash,
            raw_payload=raw_str,
            endpoint="EOD",
            start_date=date_from,
            end_date=date_to,
            output_size=None,
            is_rate_limited=is_rate_limited,
            history_refresh_required=history_refresh_required,
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
        Retrieves rolling daily EOD price data asynchronously for a European stock or ETF.
        """
        retrieved_at = datetime.now(timezone.utc)
        provider_symbol = context.provider_symbol
        canonical_id = context.canonical_instrument_id

        # 1. Preflight: Dual Identity Binding Verification
        if canonical_id and provider_symbol and resolver:
            resolved_id = resolver.resolve_provider_symbol_to_instrument_id(
                MARKETSTACK_PROVIDER_NAME, provider_symbol, as_of_date=context.effective_date
            )
            if resolved_id != canonical_id:
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
                canonical_id, MARKETSTACK_PROVIDER_NAME, as_of_date=context.effective_date
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
                warnings=["UNRESOLVED_SYMBOL: No provider_symbol supplied or resolved for Marketstack request."],
                canonical_instrument_id=canonical_id,
                provider_symbol=None,
            )

        clean_symbol = provider_symbol.strip().upper()

        # 3. Validate canonical ID, Master MIC, and Instrument Type
        expected_mic: Optional[str] = None
        if not canonical_id and resolver:
            canonical_id = resolver.resolve_provider_symbol_to_instrument_id(
                MARKETSTACK_PROVIDER_NAME, clean_symbol, as_of_date=context.effective_date
            )

        if canonical_id and resolver:
            inst = resolver.get_instrument_by_id(canonical_id)
            if inst:
                expected_mic = inst.mic
                if inst.instrument_type not in (InstrumentType.EUROPEAN_STOCK, InstrumentType.EUROPEAN_ETF):
                    return ProviderResponse(
                        provider_name=self.provider_name,
                        source_quality=self.source_quality,
                        retrieved_at=retrieved_at,
                        published_at=None,
                        effective_date=context.effective_date,
                        status=DataStatus.UNAVAILABLE,
                        raw=None,
                        warnings=[
                            f"UNSUPPORTED_INSTRUMENT_TYPE: Marketstack adapter only supports EUROPEAN_STOCK and EUROPEAN_ETF, got {inst.instrument_type}."
                        ],
                        canonical_instrument_id=canonical_id,
                        provider_symbol=clean_symbol,
                    )
        elif not canonical_id and resolver:
            # If symbol provided but not mapped in resolver -> fail closed
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"UNRESOLVED_IDENTITY: Symbol '{clean_symbol}' not mapped to canonical instrument."],
                canonical_instrument_id=None,
                provider_symbol=clean_symbol,
            )

        # 4. Check API Access Key
        access_key = self._access_key or os.getenv("MARKETSTACK_ACCESS_KEY") or os.getenv("MARKETSTACK_KEY")
        if not access_key:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["AUTH_ERROR: MARKETSTACK_ACCESS_KEY is not configured in environment or provider instance."],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        # 5. Parse and Validate Date Boundaries
        date_from_val: Optional[date] = None
        date_to_val: Optional[date] = None

        raw_from = context.request_parameters.get("date_from") or context.request_parameters.get("startDate")
        if raw_from is not None:
            date_from_val, err = _parse_marketstack_request_date(raw_from, "date_from")
            if err:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[err],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

        raw_to = context.request_parameters.get("date_to") or context.request_parameters.get("endDate")
        if raw_to is not None:
            date_to_val, err = _parse_marketstack_request_date(raw_to, "date_to")
            if err:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[err],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

        if date_from_val and date_to_val and date_from_val > date_to_val:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[
                    f"INVALID_DATE_RANGE: date_from '{date_from_val.isoformat()}' cannot be after date_to '{date_to_val.isoformat()}'."
                ],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        # Free tier history window enforcement: <= 366 days
        if date_from_val and date_to_val and (date_to_val - date_from_val).days > 366:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[
                    f"FREE_HISTORY_WINDOW_EXCEEDED: Requested range {(date_to_val - date_from_val).days} days exceeds Marketstack Free 366-day limit."
                ],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        # 6. Build Query Parameters (access_key in params only, never serialized)
        params: Dict[str, Any] = {
            "access_key": access_key,
            "symbols": clean_symbol,
            "limit": 1000,
            "sort": "ASC",
        }
        if date_from_val:
            params["date_from"] = date_from_val.isoformat()
        if date_to_val:
            params["date_to"] = date_to_val.isoformat()

        try:
            client = get_http_client()
            resp = await client.get(MARKETSTACK_BASE_URL, params=params)
            retrieved_at = datetime.now(timezone.utc)
            http_status = resp.status_code

            if http_status in (401, 403):
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"AUTH_ERROR: HTTP {http_status} authentication failure from Marketstack."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

            if http_status == 404:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"NOT_FOUND: Symbol '{clean_symbol}' not found on Marketstack."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

            if http_status == 429:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=["RATE_LIMITED: HTTP 429 Too Many Requests from Marketstack."],
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
                    warnings=[f"SERVER_ERROR: HTTP {http_status} server error from Marketstack."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

            raw_text = resp.text
            snapshot = self.parse_eod_response(
                raw_text,
                clean_symbol,
                retrieved_at,
                http_status=http_status,
                resolver=resolver,
                date_from=date_from_val,
                date_to=date_to_val,
                expected_mic=expected_mic,
            )
            if canonical_id:
                snapshot.instrument_id = canonical_id

            obs_count = len(snapshot.observations)
            valid_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.VALID)
            invalid_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.INVALID_OBSERVATION)
            unresolved_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.UNRESOLVED_IDENTITY)
            conflict_count = sum(1 for o in snapshot.observations if o.status == GlobalObservationStatus.DUPLICATE_CONFLICT)
            has_truncation = any("TRUNCATED_RESPONSE" in d or "INVALID_PAGINATION" in d for d in snapshot.diagnostics)

            if snapshot.is_rate_limited or obs_count == 0 or valid_count == 0:
                status = DataStatus.UNAVAILABLE
            elif has_truncation:
                status = DataStatus.PARTIAL
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
                    "history_refresh_required": snapshot.history_refresh_required,
                    "observation_count": obs_count,
                    "valid_count": valid_count,
                    "invalid_count": invalid_count,
                    "unresolved_count": unresolved_count,
                    "conflict_count": conflict_count,
                    "has_truncation": has_truncation,
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
                warnings=[f"TIMEOUT: Marketstack request timed out: {err}"],
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
            endpoint="EOD",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=response.source_metadata,
        )
