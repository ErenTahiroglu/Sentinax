"""
backend/engine/private/providers/tefas_eod.py
=============================================
TEFAS 2026 Turkish Investment Fund EOD Price History Core Adapter.

Access Classification:
    - Provider Access: YELLOW (Official public web endpoint; no developer SLA/quota contract).
    - Source Quality: TIER_2_EXCHANGE (Takasbank official central clearing & fund distribution platform).
    - Capabilities: PUBLIC_LOW_FREQUENCY, FUND_PRICE_HISTORY, ROLLING_5Y_HISTORY.

Production Endpoint:
    POST https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir
    Body: {"fonKodu": "<TEFAS_CODE>", "dil": "TR", "periyod": <PERIOD_MONTHS>}

Hardening Invariants:
    - Turkish Fund Scope: strictly supports InstrumentType.TEFAS_FUND, TEFAS_MONEY_MARKET,
      TEFAS_EQUITY, TEFAS_VARIABLE, TEFAS_BALANCED under AssetClass.FUND.
    - Zero float usage: all financial prices are exact Decimal parsed at the lexical JSON boundary.
    - Missing fields remain None (missing != zero).
    - Unit prices must be finite and strictly positive (> 0).
    - Rejects non-finite values (NaN, Infinity, -Infinity) and zero/negative prices.
    - Strict Point-in-Time (PIT) semantics: trade_date (economic date) vs retrieved_at (network UTC);
      published_at is None (no micro-timestamps).
    - Title metadata in time-series is CURRENT_METADATA_ONLY and does not control canonical identity.
    - Dual identity preflight: conflicting (canonical_instrument_id, provider_symbol) in FetchContext
      fails before HTTP with IDENTITY_MISMATCH.
    - Currency Authority & Scope: public TEFAS price history supports canonical Currency.TRY instruments only;
      non-TRY canonical currencies fail closed with AMBIGUOUS_PAY_GROUP_CURRENCY before HTTP.
    - Supported period set: strictly {1, 3, 6, 12, 36, 60} months. Unsupported periods fail before HTTP.
    - Deduplication: identical prices on same trade_date deduplicate; differing prices record DUPLICATE_CONFLICT.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID, uuid4

import httpx

from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.identity import InstrumentResolverService
from backend.engine.private.market_data.tefas_models import (
    TefasCapability,
    TefasFundPriceObservation,
    TefasFundPriceSnapshot,
    TefasObservationStatus,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.storage_models import compute_payload_hash
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)

TEFAS_PROVIDER_NAME = "TEFAS"
TEFAS_PROVIDER_VERSION = "1.0.0"
TEFAS_BASE_URL = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
TEFAS_SUPPORTED_PERIODS: Set[int] = {1, 3, 6, 12, 36, 60}
TEFAS_ALLOWED_INSTRUMENT_TYPES: Set[InstrumentType] = {
    InstrumentType.TEFAS_FUND,
    InstrumentType.TEFAS_MONEY_MARKET,
    InstrumentType.TEFAS_EQUITY,
    InstrumentType.TEFAS_VARIABLE,
    InstrumentType.TEFAS_BALANCED,
}


def _parse_finite_decimal(value: Any) -> Optional[Decimal]:
    """
    Parses a string or integer value into an exact finite Decimal.
    STRICT: Rejects Python float inputs and comma-formatted strings to prevent loss-of-precision or misinterpretation.
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
        val_str = value.strip()
        if not val_str or "," in val_str or val_str.lower() in ("none", "null", "nan", "inf", "-inf", "infinity", "-infinity"):
            return None
        try:
            d = Decimal(val_str)
            if not d.is_finite():
                return None
            return d
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


class TefasFundPriceProvider(DataProviderContract):
    """
    Data provider adapter for TEFAS Turkish Investment Fund EOD Price History.
    """
    provider_name: str = TEFAS_PROVIDER_NAME
    provider_version: str = TEFAS_PROVIDER_VERSION
    source_quality: SourceTier = SourceTier.TIER_2_EXCHANGE
    access_status: ProviderAccessStatus = ProviderAccessStatus.YELLOW

    official_source: bool = True
    developer_api: bool = False
    sla_guaranteed: bool = False

    capabilities: List[TefasCapability] = [
        TefasCapability.PUBLIC_LOW_FREQUENCY,
        TefasCapability.FUND_PRICE_HISTORY,
        TefasCapability.ROLLING_5Y_HISTORY,
    ]

    def __init__(self, resolver: Optional[InstrumentResolverService] = None) -> None:
        self._resolver = resolver

    # ─────────────────────────────────────────────────────────────────────────
    # Parsing & Normalization
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def parse_daily_prices(
        cls,
        raw_json_or_text: Any,
        provider_symbol: str,
        retrieved_at: datetime,
        http_status: int = 200,
        period_months: int = 1,
        resolver: Optional[InstrumentResolverService] = None,
        snapshot_id: Optional[UUID] = None,
        canonical_instrument_id: Optional[UUID] = None,
    ) -> TefasFundPriceSnapshot:
        """
        Parses raw TEFAS daily fund prices JSON response into a TefasFundPriceSnapshot
        and normalized TefasFundPriceObservation instances.
        """
        snap_id = snapshot_id or uuid4()
        clean_symbol = provider_symbol.strip().upper()

        if isinstance(raw_json_or_text, str):
            raw_text = raw_json_or_text
            try:
                data = json.loads(raw_text, parse_float=Decimal)
            except Exception as err:
                return TefasFundPriceSnapshot(
                    id=snap_id,
                    provider=TEFAS_PROVIDER_NAME,
                    provider_symbol=clean_symbol,
                    retrieved_at=retrieved_at,
                    http_status=http_status,
                    payload_hash=compute_payload_hash(raw_text),
                    raw_payload=raw_text,
                    instrument_id=canonical_instrument_id,
                    period_months=period_months,
                    diagnostics=[f"MALFORMED_JSON: Failed to parse raw response as JSON: {err}"],
                )
        elif isinstance(raw_json_or_text, dict):
            data = raw_json_or_text
            raw_text = json.dumps(raw_json_or_text, default=str)
        else:
            raw_text = str(raw_json_or_text)
            return TefasFundPriceSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=compute_payload_hash(raw_text),
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                period_months=period_months,
                diagnostics=[f"SCHEMA_MISMATCH: Expected dict or JSON string, got {type(raw_json_or_text).__name__}"],
            )

        payload_hash = compute_payload_hash(raw_text)
        diagnostics: List[str] = []

        if not isinstance(data, dict):
            return TefasFundPriceSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                period_months=period_months,
                diagnostics=[f"SCHEMA_MISMATCH: Root JSON must be an object, got {type(data).__name__}."],
            )

        # 1. Error Envelope Check
        error_code = data.get("errorCode")
        error_message = data.get("errorMessage")
        if error_code is not None or (error_message and str(error_message).strip()):
            err_diag = f"ERROR_ENVELOPE: TEFAS returned error code '{error_code}', message: '{error_message}'."
            return TefasFundPriceSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                period_months=period_months,
                diagnostics=[err_diag],
            )

        # 2. Result List Check
        result_list = data.get("resultList")
        if result_list is None or not isinstance(result_list, list):
            return TefasFundPriceSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                period_months=period_months,
                diagnostics=["SCHEMA_MISMATCH: Root 'resultList' is missing or not a list."],
            )

        if len(result_list) == 0:
            return TefasFundPriceSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                period_months=period_months,
                diagnostics=["EMPTY_RESPONSE: 'resultList' contains 0 price rows."],
            )

        # 3. Observation Parsing & Deduplication
        observations: List[TefasFundPriceObservation] = []
        seen_observations_by_date: Dict[date, Decimal] = {}
        source_row_count = len(result_list)
        malformed_row_count = 0

        for idx, row in enumerate(result_list):
            row_diags: List[str] = []
            status = TefasObservationStatus.VALID

            if not isinstance(row, dict):
                malformed_row_count += 1
                diagnostics.append(f"MALFORMED_ROW: Row {idx} is not an object: {row}.")
                continue

            # Provider Symbol Validation
            raw_fon_kodu = row.get("fonKodu")
            row_symbol = str(raw_fon_kodu or "").strip().upper()
            if not row_symbol or row_symbol != clean_symbol:
                status = TefasObservationStatus.INVALID_SOURCE_CONTEXT
                row_diags.append(
                    f"SYMBOL_MISMATCH: Row symbol '{row_symbol}' does not match requested '{clean_symbol}'."
                )

            # Trade Date Parsing (Strict ISO YYYY-MM-DD)
            raw_tarih = row.get("tarih")
            t_date: Optional[date] = None
            if not raw_tarih:
                status = TefasObservationStatus.INVALID_OBSERVATION
                row_diags.append("MISSING_DATE: Field 'tarih' is missing or empty.")
            else:
                try:
                    t_date = date.fromisoformat(str(raw_tarih).strip())
                except (ValueError, TypeError) as err:
                    status = TefasObservationStatus.INVALID_OBSERVATION
                    row_diags.append(f"INVALID_DATE: Field 'tarih' value '{raw_tarih}' invalid: {err}.")

            # Unit Price Parsing (Strict lexical Decimal > 0)
            raw_fiyat = row.get("fiyat")
            unit_price_val = _parse_finite_decimal(raw_fiyat)
            if unit_price_val is None:
                status = TefasObservationStatus.INVALID_OBSERVATION
                row_diags.append(f"MISSING_PRICE: Field 'fiyat' is missing, non-finite, or float: {raw_fiyat}.")
            elif unit_price_val <= Decimal("0"):
                status = TefasObservationStatus.INVALID_OBSERVATION
                row_diags.append(f"NON_POSITIVE_PRICE: Field 'fiyat' must be strictly positive: {unit_price_val}.")

            # Duplicate Check
            if t_date is not None:
                if t_date in seen_observations_by_date:
                    existing_price = seen_observations_by_date[t_date]
                    if unit_price_val is not None and existing_price == unit_price_val:
                        # Deterministic deduplication: skip duplicate identical observation
                        continue
                    else:
                        status = TefasObservationStatus.DUPLICATE_CONFLICT
                        row_diags.append(
                            f"DUPLICATE_CONFLICT: Differing price on date {t_date.isoformat()}: {existing_price} vs {unit_price_val}."
                        )
                elif unit_price_val is not None and status == TefasObservationStatus.VALID:
                    seen_observations_by_date[t_date] = unit_price_val

            # Master Instrument Resolution & Currency
            resolved_inst_id: Optional[UUID] = canonical_instrument_id
            resolved_inst_type: Optional[InstrumentType] = None
            resolved_currency: Optional[Currency] = None

            if resolver and t_date:
                matched_id = resolver.resolve_provider_symbol_to_instrument_id(
                    TEFAS_PROVIDER_NAME, clean_symbol, as_of_date=t_date
                )
                if matched_id:
                    resolved_inst_id = matched_id
                    inst = resolver.get_instrument_by_id(matched_id)
                    if inst:
                        resolved_inst_type = inst.instrument_type
                        resolved_currency = inst.currency

                        if (
                            inst.asset_class != AssetClass.FUND
                            or resolved_inst_type not in TEFAS_ALLOWED_INSTRUMENT_TYPES
                        ):
                            status = TefasObservationStatus.INVALID_OBSERVATION
                            row_diags.append(
                                f"UNSUPPORTED_INSTRUMENT_TYPE: Master instrument type '{resolved_inst_type}' not supported by TEFAS provider."
                            )
                        if resolved_currency is None:
                            status = TefasObservationStatus.UNRESOLVED_IDENTITY
                            row_diags.append("MISSING_CURRENCY: Master instrument has no currency defined.")
                        elif resolved_currency != Currency.TRY:
                            status = TefasObservationStatus.INVALID_SOURCE_CONTEXT
                            row_diags.append(
                                f"AMBIGUOUS_PAY_GROUP_CURRENCY: TEFAS public prices only support TRY canonical instruments; got {resolved_currency.value}."
                            )
                else:
                    if status == TefasObservationStatus.VALID:
                        status = TefasObservationStatus.UNRESOLVED_IDENTITY
                    row_diags.append(f"UNRESOLVED_IDENTITY: No master instrument mapped for TEFAS:{clean_symbol} on {t_date.isoformat()}.")
            elif canonical_instrument_id and resolver:
                inst = resolver.get_instrument_by_id(canonical_instrument_id)
                if inst:
                    resolved_inst_type = inst.instrument_type
                    resolved_currency = inst.currency
                    if (
                        inst.asset_class != AssetClass.FUND
                        or resolved_inst_type not in TEFAS_ALLOWED_INSTRUMENT_TYPES
                    ):
                        status = TefasObservationStatus.INVALID_OBSERVATION
                        row_diags.append(
                            f"UNSUPPORTED_INSTRUMENT_TYPE: Master instrument type '{resolved_inst_type}' not supported by TEFAS provider."
                        )
                    if resolved_currency is None:
                        status = TefasObservationStatus.UNRESOLVED_IDENTITY
                        row_diags.append("MISSING_CURRENCY: Master instrument has no currency defined.")
                    elif resolved_currency != Currency.TRY:
                        status = TefasObservationStatus.INVALID_SOURCE_CONTEXT
                        row_diags.append(
                            f"AMBIGUOUS_PAY_GROUP_CURRENCY: TEFAS public prices only support TRY canonical instruments; got {resolved_currency.value}."
                        )
            elif not resolver:
                if status == TefasObservationStatus.VALID:
                    status = TefasObservationStatus.UNRESOLVED_IDENTITY
                row_diags.append(f"UNRESOLVED_IDENTITY: No InstrumentResolverService provided for TEFAS:{clean_symbol}.")

            if t_date is None:
                # If date could not be parsed, assign a dummy date so dataclass holds a date
                t_date = retrieved_at.date()

            obs = TefasFundPriceObservation(
                provider_symbol=clean_symbol,
                trade_date=t_date,
                unit_price=unit_price_val,
                currency=resolved_currency,
                instrument_id=resolved_inst_id,
                instrument_type=resolved_inst_type,
                provider=TEFAS_PROVIDER_NAME,
                snapshot_id=snap_id,
                payload_hash=payload_hash,
                retrieved_at=retrieved_at,
                published_at=None,
                status=status,
                confidence_level=DataConfidenceLevel.MEDIUM,
                diagnostics=row_diags,
            )
            observations.append(obs)

        # 4. Determine Actual Valid Trade Date Range
        valid_dates = [o.trade_date for o in observations if o.is_valid]
        trade_date_range: Tuple[Optional[date], Optional[date]] = (
            (min(valid_dates), max(valid_dates)) if valid_dates else (None, None)
        )

        return TefasFundPriceSnapshot(
            id=snap_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol=clean_symbol,
            retrieved_at=retrieved_at,
            http_status=http_status,
            payload_hash=payload_hash,
            raw_payload=raw_text,
            instrument_id=canonical_instrument_id,
            period_months=period_months,
            endpoint="FUND_PRICE_HISTORY",
            source_row_count=source_row_count,
            malformed_row_count=malformed_row_count,
            trade_date_range=trade_date_range,
            observations=observations,
            diagnostics=diagnostics,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # DataProviderContract Execution
    # ─────────────────────────────────────────────────────────────────────────

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Executes a low-frequency HTTP POST request to TEFAS for fund price history.
        """
        retrieved_at = datetime.now(timezone.utc)
        clean_symbol = (context.provider_symbol or "").strip().upper()
        canonical_id = context.canonical_instrument_id
        request_params = context.request_parameters or {}
        resolver = request_params.get("resolver") or self._resolver

        # 1. Period Validation
        period_months = request_params.get("period_months", 1)
        if not isinstance(period_months, int) or period_months not in TEFAS_SUPPORTED_PERIODS:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[
                    f"INVALID_PERIOD: period_months '{period_months}' is invalid. Supported: {sorted(TEFAS_SUPPORTED_PERIODS)}."
                ],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        # 2. Identity Preflight & Validation
        if resolver:
            if canonical_id and clean_symbol:
                # Dual Identity Preflight
                resolved_id = resolver.resolve_provider_symbol_to_instrument_id(
                    self.provider_name, clean_symbol, as_of_date=context.effective_date
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
                            f"IDENTITY_MISMATCH: Provided canonical_instrument_id '{canonical_id}' does not match resolved '{resolved_id}' for TEFAS:{clean_symbol}."
                        ],
                        canonical_instrument_id=canonical_id,
                        provider_symbol=clean_symbol,
                    )
            elif canonical_id and not clean_symbol:
                alias = resolver.resolve_instrument_id_to_provider_symbol(
                    canonical_id, self.provider_name, as_of_date=context.effective_date
                )
                if not alias:
                    return ProviderResponse(
                        provider_name=self.provider_name,
                        source_quality=self.source_quality,
                        retrieved_at=retrieved_at,
                        published_at=None,
                        effective_date=context.effective_date,
                        status=DataStatus.UNAVAILABLE,
                        raw=None,
                        warnings=[f"UNRESOLVED_IDENTITY: No TEFAS alias for instrument '{canonical_id}'."],
                        canonical_instrument_id=canonical_id,
                        provider_symbol="",
                    )
                clean_symbol = alias.strip().upper()
            elif clean_symbol and not canonical_id:
                resolved_id = resolver.resolve_provider_symbol_to_instrument_id(
                    self.provider_name, clean_symbol, as_of_date=context.effective_date
                )
                if not resolved_id:
                    return ProviderResponse(
                        provider_name=self.provider_name,
                        source_quality=self.source_quality,
                        retrieved_at=retrieved_at,
                        published_at=None,
                        effective_date=context.effective_date,
                        status=DataStatus.UNAVAILABLE,
                        raw=None,
                        warnings=[f"UNRESOLVED_IDENTITY: No canonical instrument for TEFAS:{clean_symbol}."],
                        canonical_instrument_id=None,
                        provider_symbol=clean_symbol,
                    )
                canonical_id = resolved_id

            # Instrument Type & Currency Validation
            if canonical_id:
                inst = resolver.get_instrument_by_id(canonical_id)
                if inst:
                    if inst.asset_class != AssetClass.FUND or inst.instrument_type not in TEFAS_ALLOWED_INSTRUMENT_TYPES:
                        return ProviderResponse(
                            provider_name=self.provider_name,
                            source_quality=self.source_quality,
                            retrieved_at=retrieved_at,
                            published_at=None,
                            effective_date=context.effective_date,
                            status=DataStatus.UNAVAILABLE,
                            raw=None,
                            warnings=[
                                f"UNSUPPORTED_INSTRUMENT_TYPE: TEFAS provider only supports AssetClass.FUND and types {sorted(t.value for t in TEFAS_ALLOWED_INSTRUMENT_TYPES)}, got {inst.instrument_type}."
                            ],
                            canonical_instrument_id=canonical_id,
                            provider_symbol=clean_symbol,
                        )
                    if inst.currency is None:
                        return ProviderResponse(
                            provider_name=self.provider_name,
                            source_quality=self.source_quality,
                            retrieved_at=retrieved_at,
                            published_at=None,
                            effective_date=context.effective_date,
                            status=DataStatus.UNAVAILABLE,
                            raw=None,
                            warnings=["MISSING_CURRENCY: Canonical instrument has no currency defined in Instrument Master."],
                            canonical_instrument_id=canonical_id,
                            provider_symbol=clean_symbol,
                        )
                    if inst.currency != Currency.TRY:
                        return ProviderResponse(
                            provider_name=self.provider_name,
                            source_quality=self.source_quality,
                            retrieved_at=retrieved_at,
                            published_at=None,
                            effective_date=context.effective_date,
                            status=DataStatus.UNAVAILABLE,
                            raw=None,
                            warnings=[
                                f"AMBIGUOUS_PAY_GROUP_CURRENCY: TEFAS public price history only supports canonical TRY instruments; got {inst.currency.value} for instrument '{canonical_id}' (TEFAS:{clean_symbol})."
                            ],
                            canonical_instrument_id=canonical_id,
                            provider_symbol=clean_symbol,
                        )

        if not clean_symbol:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=context.effective_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["UNRESOLVED_IDENTITY: Missing provider symbol."],
                canonical_instrument_id=canonical_id,
                provider_symbol="",
            )

        # 3. HTTP Request
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Sentinax/1.0 (Personal Portfolio Engine)",
            "Origin": "https://www.tefas.gov.tr",
            "Referer": "https://www.tefas.gov.tr/tr/fon-getirileri",
        }
        body_payload = {
            "fonKodu": clean_symbol,
            "dil": "TR",
            "periyod": period_months,
        }

        try:
            client = get_http_client()
            resp = await client.post(
                TEFAS_BASE_URL,
                json=body_payload,
                headers=headers,
                timeout=15.0,
            )
            http_status = resp.status_code

            if http_status == 429:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=["RATE_LIMITED: TEFAS upstream rate limit exceeded (HTTP 429)."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                    source_metadata={"is_rate_limited": True},
                )
            if http_status == 403:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=["ACCESS_BLOCKED: TEFAS request blocked or forbidden (HTTP 403)."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )
            if http_status != 200:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=None,
                    effective_date=context.effective_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"UPSTREAM_ERROR: TEFAS HTTP {http_status}."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

            raw_text = resp.text
            snapshot = self.parse_daily_prices(
                raw_text,
                clean_symbol,
                retrieved_at,
                http_status=http_status,
                period_months=period_months,
                resolver=resolver,
                canonical_instrument_id=canonical_id,
            )

            source_row_count = snapshot.source_row_count
            malformed_row_count = snapshot.malformed_row_count
            obs_count = len(snapshot.observations)
            valid_count = sum(1 for o in snapshot.observations if o.is_valid)
            invalid_count = sum(1 for o in snapshot.observations if o.status == TefasObservationStatus.INVALID_OBSERVATION)
            unresolved_count = sum(1 for o in snapshot.observations if o.status == TefasObservationStatus.UNRESOLVED_IDENTITY)
            conflict_count = sum(1 for o in snapshot.observations if o.status == TefasObservationStatus.DUPLICATE_CONFLICT)
            context_mismatch_count = sum(1 for o in snapshot.observations if o.status == TefasObservationStatus.INVALID_SOURCE_CONTEXT)

            if snapshot.is_rate_limited or valid_count == 0:
                status = DataStatus.UNAVAILABLE
            elif (
                valid_count > 0
                and malformed_row_count == 0
                and invalid_count == 0
                and unresolved_count == 0
                and conflict_count == 0
                and context_mismatch_count == 0
            ):
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
                    "source_row_count": source_row_count,
                    "parsed_observation_count": obs_count,
                    "malformed_row_count": malformed_row_count,
                    "valid_count": valid_count,
                    "invalid_count": invalid_count,
                    "unresolved_count": unresolved_count,
                    "conflict_count": conflict_count,
                    "context_mismatch_count": context_mismatch_count,
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
                warnings=[f"TIMEOUT: TEFAS request timed out: {err}"],
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
                warnings=[f"NETWORK_ERROR: TEFAS network error: {err}"],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """Maps raw snapshot or observation to a canonical field dict."""
        if isinstance(raw, TefasFundPriceSnapshot):
            return raw.to_dict()
        if isinstance(raw, TefasFundPriceObservation):
            return raw.to_dict()
        if isinstance(raw, dict):
            return raw
        return {"raw": str(raw)}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        """Returns warnings for anomalies or missing fields."""
        warnings: List[str] = []
        if "unit_price" in normalized and normalized["unit_price"] is None:
            warnings.append("Missing unit price.")
        if normalized.get("status") == TefasObservationStatus.UNRESOLVED_IDENTITY.value:
            warnings.append("Unresolved instrument identity.")
        if normalized.get("status") == TefasObservationStatus.INVALID_OBSERVATION.value:
            warnings.append("Invalid observation.")
        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        """Returns provenance audit trail for a ProviderResponse."""
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint="FUND_PRICE_HISTORY",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=response.source_metadata,
        )
