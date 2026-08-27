"""
backend/engine/private/providers/tefas_metrics.py
=================================================
TEFAS 2026 Turkish Investment Fund Current Valuation and Metrics Core Adapter.

Access Classification:
    - Provider Access: YELLOW (Official public web endpoint; no developer SLA/quota contract).
    - Source Quality: TIER_2_EXCHANGE (Takasbank official central clearing & fund distribution platform).
    - Capabilities: PUBLIC_LOW_FREQUENCY.

Production Endpoint:
    POST https://www.tefas.gov.tr/api/funds/fonBilgiGetir
    Body: {"fonKodu": "<TEFAS_CODE>", "dil": "TR"}

Hardening Invariants:
    - Turkish Fund Scope: strictly supports InstrumentType.TEFAS_FUND, TEFAS_MONEY_MARKET,
      TEFAS_EQUITY, TEFAS_VARIABLE, TEFAS_BALANCED under AssetClass.FUND.
    - Zero float usage: all financial values are exact Decimal parsed at the lexical JSON boundary.
    - Missing fields remain None (missing != zero).
    - Non-negative value rules: portfolio_size >= 0, outstanding_units >= 0, investor_count >= 0.
    - Zero values (0) are valid non-negative states and not converted to missing.
    - Strict Point-in-Time (PIT) semantics: retrieved_at is network UTC; published_at and effective_date are strictly None.
    - Identity resolution uses retrieved_at.date() as reference date only; never fabricates effective_date.
    - Multi-pay-group safety: supports canonical Currency.TRY instruments only; non-TRY currencies fail closed
      with AMBIGUOUS_PAY_GROUP_CURRENCY before HTTP dispatch.
    - Identity preflight: requires InstrumentResolverService; conflicting (canonical_instrument_id, provider_symbol)
      fails before HTTP with IDENTITY_MISMATCH.
    - Single current row contract: resultList must contain exactly 1 row; multiple rows fail closed with MULTIPLE_CURRENT_ROWS.
    - sonFiyat is DIAGNOSTIC CROSS-CHECK ONLY and does not control COMPLETE/PARTIAL status.
    - Category / rank / return / market share remain raw snapshot context only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set
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
from backend.engine.private.market_data.tefas_metrics_models import (
    TefasFundCurrentMetricsObservation,
    TefasFundMetricsSnapshot,
)
from backend.engine.private.market_data.tefas_models import (
    TefasCapability,
    TefasObservationStatus,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.storage_models import (
    NormalizedObservationRecord,
    compute_payload_hash,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)

TEFAS_PROVIDER_NAME = "TEFAS"
TEFAS_PROVIDER_VERSION = "1.0.0"
TEFAS_METRICS_BASE_URL = "https://www.tefas.gov.tr/api/funds/fonBilgiGetir"
TEFAS_ALLOWED_INSTRUMENT_TYPES: Set[InstrumentType] = {
    InstrumentType.TEFAS_FUND,
    InstrumentType.TEFAS_MONEY_MARKET,
    InstrumentType.TEFAS_EQUITY,
    InstrumentType.TEFAS_VARIABLE,
    InstrumentType.TEFAS_BALANCED,
}


def _parse_finite_non_negative_decimal(value: Any) -> Optional[Decimal]:
    """
    Parses a string, integer, or Decimal into an exact finite non-negative Decimal (>= 0).
    STRICT: Rejects booleans, Python floats, negative numbers, and comma-formatted strings.
    """
    if value is None or isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, (int, Decimal)):
        d = Decimal(value)
        return d if d.is_finite() and d >= Decimal("0") else None
    if isinstance(value, str):
        val_str = value.strip()
        if not val_str or "," in val_str or val_str.lower() in ("none", "null", "nan", "inf", "-inf", "infinity", "-infinity"):
            return None
        try:
            d = Decimal(val_str)
            return d if d.is_finite() and d >= Decimal("0") else None
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


def _parse_finite_positive_decimal(value: Any) -> Optional[Decimal]:
    """
    Parses a string, integer, or Decimal into an exact finite strictly positive Decimal (> 0).
    """
    d = _parse_finite_non_negative_decimal(value)
    return d if d is not None and d > Decimal("0") else None


def _parse_non_negative_integer(value: Any) -> Optional[int]:
    """
    Parses an integer or integer-equivalent Decimal/string into a non-negative int (>= 0).
    STRICT: Rejects booleans, floats, fractional Decimals, negative values, and non-digit strings.
    """
    if value is None or isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, Decimal):
        if value.is_finite() and value >= Decimal("0") and value == value.to_integral_value():
            return int(value)
        return None
    if isinstance(value, str):
        val_str = value.strip()
        if not val_str or not val_str.isdigit():
            return None
        try:
            val = int(val_str)
            return val if val >= 0 else None
        except (ValueError, TypeError):
            return None
    return None


class TefasFundCurrentMetricsProvider(DataProviderContract):
    """
    Data provider adapter for TEFAS Turkish Investment Fund Current Valuation and Metrics (AUM, Investors, Units).
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
    ]

    def __init__(self, resolver: Optional[InstrumentResolverService] = None) -> None:
        self._resolver = resolver

    # ─────────────────────────────────────────────────────────────────────────
    # Parsing & Normalization
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def parse_current_metrics(
        cls,
        raw_json_or_text: Any,
        provider_symbol: str,
        retrieved_at: datetime,
        http_status: int = 200,
        resolver: Optional[InstrumentResolverService] = None,
        snapshot_id: Optional[UUID] = None,
        canonical_instrument_id: Optional[UUID] = None,
    ) -> TefasFundMetricsSnapshot:
        """
        Parses raw TEFAS fonBilgiGetir JSON response into a TefasFundMetricsSnapshot
        and normalized TefasFundCurrentMetricsObservation instance.
        """
        snap_id = snapshot_id or uuid4()
        clean_symbol = provider_symbol.strip().upper()

        if isinstance(raw_json_or_text, str):
            raw_text = raw_json_or_text
            try:
                data = json.loads(raw_text, parse_float=Decimal)
            except Exception as err:
                return TefasFundMetricsSnapshot(
                    id=snap_id,
                    provider=TEFAS_PROVIDER_NAME,
                    provider_symbol=clean_symbol,
                    retrieved_at=retrieved_at,
                    http_status=http_status,
                    payload_hash=compute_payload_hash(raw_text),
                    raw_payload=raw_text,
                    instrument_id=canonical_instrument_id,
                    diagnostics=[f"MALFORMED_JSON: Failed to parse raw response as JSON: {err}"],
                )
        elif isinstance(raw_json_or_text, dict):
            data = raw_json_or_text
            raw_text = json.dumps(raw_json_or_text, default=str)
        else:
            raw_text = str(raw_json_or_text)
            return TefasFundMetricsSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=compute_payload_hash(raw_text),
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                diagnostics=[f"SCHEMA_MISMATCH: Expected dict or JSON string, got {type(raw_json_or_text).__name__}"],
            )

        payload_hash = compute_payload_hash(raw_text)
        diagnostics: List[str] = []

        if not isinstance(data, dict):
            return TefasFundMetricsSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                diagnostics=[f"SCHEMA_MISMATCH: Root JSON must be an object, got {type(data).__name__}."],
            )

        # 1. Error Envelope Check
        error_code = data.get("errorCode")
        error_message = data.get("errorMessage")
        if error_code is not None or (error_message and str(error_message).strip()):
            err_diag = f"ERROR_ENVELOPE: TEFAS returned error code '{error_code}', message: '{error_message}'."
            return TefasFundMetricsSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                diagnostics=[err_diag],
            )

        # 2. Result List Check
        result_list = data.get("resultList")
        if result_list is None or not isinstance(result_list, list):
            return TefasFundMetricsSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                diagnostics=["SCHEMA_MISMATCH: Root 'resultList' is missing or not a list."],
            )

        if len(result_list) == 0:
            return TefasFundMetricsSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                diagnostics=["EMPTY_RESPONSE: 'resultList' contains 0 fund metric rows."],
            )

        if len(result_list) > 1:
            return TefasFundMetricsSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                diagnostics=[f"MULTIPLE_CURRENT_ROWS: Expected exactly 1 current row for {clean_symbol}, got {len(result_list)}."],
            )

        row = result_list[0]
        if not isinstance(row, dict):
            return TefasFundMetricsSnapshot(
                id=snap_id,
                provider=TEFAS_PROVIDER_NAME,
                provider_symbol=clean_symbol,
                retrieved_at=retrieved_at,
                http_status=http_status,
                payload_hash=payload_hash,
                raw_payload=raw_text,
                instrument_id=canonical_instrument_id,
                diagnostics=[f"MALFORMED_ROW: Row is not an object: {row}."],
            )

        # 3. Provider Symbol Validation
        row_diags: List[str] = []
        status = TefasObservationStatus.VALID

        raw_fon_kodu = row.get("fonKodu")
        row_symbol = str(raw_fon_kodu or "").strip().upper()
        if not row_symbol or row_symbol != clean_symbol:
            status = TefasObservationStatus.INVALID_SOURCE_CONTEXT
            row_diags.append(
                f"SYMBOL_MISMATCH: Row symbol '{row_symbol}' does not match requested '{clean_symbol}'."
            )

        # 4. Field Parsing
        # Primary Metric: portfolio_size (portBuyukluk)
        raw_port_buyukluk = row.get("portBuyukluk")
        portfolio_size_val = _parse_finite_non_negative_decimal(raw_port_buyukluk)
        if portfolio_size_val is None:
            status = TefasObservationStatus.INVALID_OBSERVATION
            row_diags.append(f"MISSING_OR_INVALID_PORTFOLIO_SIZE: 'portBuyukluk' is missing, negative, or invalid: {raw_port_buyukluk}.")

        # Secondary Metric: outstanding_units (payAdet)
        raw_pay_adet = row.get("payAdet")
        outstanding_units_val = _parse_finite_non_negative_decimal(raw_pay_adet)
        if outstanding_units_val is None:
            row_diags.append(f"MISSING_OR_INVALID_OUTSTANDING_UNITS: 'payAdet' is missing, negative, or invalid: {raw_pay_adet}.")

        # Secondary Metric: investor_count (yatirimciSayi)
        raw_yatirimci_sayi = row.get("yatirimciSayi")
        investor_count_val = _parse_non_negative_integer(raw_yatirimci_sayi)
        if investor_count_val is None:
            row_diags.append(f"MISSING_OR_INVALID_INVESTOR_COUNT: 'yatirimciSayi' is missing, negative, or non-integer: {raw_yatirimci_sayi}.")

        # Diagnostic Metric: reported_current_unit_price (sonFiyat)
        raw_son_fiyat = row.get("sonFiyat")
        reported_unit_price_val = _parse_finite_positive_decimal(raw_son_fiyat)
        if reported_unit_price_val is None and raw_son_fiyat is not None:
            row_diags.append(f"INVALID_REPORTED_CURRENT_PRICE: 'sonFiyat' is non-positive or invalid: {raw_son_fiyat}.")

        # Diagnostic Accounting Reconciliation: sonFiyat * payAdet vs portBuyukluk
        reconciliation_abs_diff: Optional[Decimal] = None
        reconciliation_rel_diff: Optional[Decimal] = None
        if (
            portfolio_size_val is not None
            and outstanding_units_val is not None
            and reported_unit_price_val is not None
        ):
            calc_aum = reported_unit_price_val * outstanding_units_val
            reconciliation_abs_diff = abs(calc_aum - portfolio_size_val)
            if portfolio_size_val > Decimal("0"):
                reconciliation_rel_diff = reconciliation_abs_diff / portfolio_size_val
            else:
                reconciliation_rel_diff = Decimal("0")

        # 5. Canonical Instrument Resolution & Currency Validation
        resolved_inst_id: Optional[UUID] = canonical_instrument_id
        resolved_inst_type: Optional[InstrumentType] = None
        resolved_currency: Optional[Currency] = None

        ref_date = retrieved_at.date()

        if resolver:
            if canonical_instrument_id:
                inst = resolver.get_instrument_by_id(canonical_instrument_id)
            else:
                matched_id = resolver.resolve_provider_symbol_to_instrument_id(
                    TEFAS_PROVIDER_NAME, clean_symbol, as_of_date=ref_date
                )
                if matched_id:
                    resolved_inst_id = matched_id
                    inst = resolver.get_instrument_by_id(matched_id)
                else:
                    inst = None

            if inst:
                resolved_inst_id = inst.id
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
                        f"AMBIGUOUS_PAY_GROUP_CURRENCY: TEFAS public metrics only support TRY canonical instruments; got {resolved_currency.value}."
                    )
            else:
                if status == TefasObservationStatus.VALID:
                    status = TefasObservationStatus.UNRESOLVED_IDENTITY
                row_diags.append(f"UNRESOLVED_IDENTITY: No master instrument mapped for TEFAS:{clean_symbol} on {ref_date.isoformat()}.")
        elif canonical_instrument_id:
            # Without resolver, cannot verify instrument type or currency
            if status == TefasObservationStatus.VALID:
                status = TefasObservationStatus.UNRESOLVED_IDENTITY
            row_diags.append(f"UNRESOLVED_IDENTITY: No InstrumentResolverService provided for canonical instrument '{canonical_instrument_id}'.")
        else:
            if status == TefasObservationStatus.VALID:
                status = TefasObservationStatus.UNRESOLVED_IDENTITY
            row_diags.append(f"UNRESOLVED_IDENTITY: No InstrumentResolverService provided for TEFAS:{clean_symbol}.")

        obs = TefasFundCurrentMetricsObservation(
            provider_symbol=clean_symbol,
            portfolio_size=portfolio_size_val,
            portfolio_size_currency=resolved_currency,
            outstanding_units=outstanding_units_val,
            investor_count=investor_count_val,
            reported_current_unit_price=reported_unit_price_val,
            instrument_id=resolved_inst_id,
            instrument_type=resolved_inst_type,
            provider=TEFAS_PROVIDER_NAME,
            snapshot_id=snap_id,
            payload_hash=payload_hash,
            retrieved_at=retrieved_at,
            published_at=None,
            effective_date=None,
            status=status,
            confidence_level=DataConfidenceLevel.MEDIUM,
            diagnostics=row_diags,
        )

        return TefasFundMetricsSnapshot(
            id=snap_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol=clean_symbol,
            retrieved_at=retrieved_at,
            http_status=http_status,
            payload_hash=payload_hash,
            raw_payload=raw_text,
            instrument_id=resolved_inst_id,
            endpoint="FUND_CURRENT_METRICS",
            observation=obs,
            diagnostics=diagnostics,
            reconciliation_absolute_diff=reconciliation_abs_diff,
            reconciliation_relative_diff=reconciliation_rel_diff,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # DataProviderContract Execution
    # ─────────────────────────────────────────────────────────────────────────

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Executes a low-frequency HTTP POST request to TEFAS for current fund metrics (AUM, Investors, Units).
        """
        retrieved_at = datetime.now(timezone.utc)
        clean_symbol = (context.provider_symbol or "").strip().upper()
        canonical_id = context.canonical_instrument_id
        request_params = context.request_parameters or {}
        resolver = request_params.get("resolver") or self._resolver

        # 1. Resolver Requirement (Mandatory for Current Metrics)
        if not resolver:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["UNRESOLVED_IDENTITY: No InstrumentResolverService provided for current metrics ingestion."],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        ref_date = retrieved_at.date()

        # 2. Identity Preflight & Validation
        if canonical_id and clean_symbol:
            # Dual Identity Preflight
            resolved_id = resolver.resolve_provider_symbol_to_instrument_id(
                self.provider_name, clean_symbol, as_of_date=ref_date
            )
            if resolved_id != canonical_id:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=None,
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
                canonical_id, self.provider_name, as_of_date=ref_date
            )
            if not alias:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"UNRESOLVED_IDENTITY: No TEFAS alias for instrument '{canonical_id}'."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol="",
                )
            clean_symbol = alias.strip().upper()
        elif clean_symbol and not canonical_id:
            resolved_id = resolver.resolve_provider_symbol_to_instrument_id(
                self.provider_name, clean_symbol, as_of_date=ref_date
            )
            if not resolved_id:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"UNRESOLVED_IDENTITY: No canonical instrument for TEFAS:{clean_symbol}."],
                    canonical_instrument_id=None,
                    provider_symbol=clean_symbol,
                )
            canonical_id = resolved_id

        # 3. Instrument Type & Currency Validation
        if canonical_id:
            inst = resolver.get_instrument_by_id(canonical_id)
            if not inst:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"UNRESOLVED_IDENTITY: Instrument '{canonical_id}' not found in master."],
                    canonical_instrument_id=canonical_id,
                    provider_symbol=clean_symbol,
                )

            if inst.asset_class != AssetClass.FUND or inst.instrument_type not in TEFAS_ALLOWED_INSTRUMENT_TYPES:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=retrieved_at,
                    published_at=None,
                    effective_date=None,
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
                    effective_date=None,
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
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[
                        f"AMBIGUOUS_PAY_GROUP_CURRENCY: TEFAS public metrics only support canonical TRY instruments; got {inst.currency.value} for instrument '{canonical_id}' (TEFAS:{clean_symbol})."
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
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["UNRESOLVED_IDENTITY: Missing provider symbol."],
                canonical_instrument_id=canonical_id,
                provider_symbol="",
            )

        # 4. HTTP Request Preparation
        request_body = {
            "fonKodu": clean_symbol,
            "dil": "TR",
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Sentinax/1.0 (Personal Portfolio Engine)",
            "Origin": "https://www.tefas.gov.tr",
            "Referer": "https://www.tefas.gov.tr/TarihselVeriler.aspx",
        }

        # 5. Network Execution (Single Attempt, Fail Closed)
        http_client = get_http_client()
        try:
            response = await http_client.post(
                TEFAS_METRICS_BASE_URL,
                json=request_body,
                headers=headers,
                timeout=10.0,
            )
            http_status = response.status_code
            raw_text = response.text
        except httpx.TimeoutException as err:
            logger.warning(f"TEFAS metrics request timed out for {clean_symbol}: {err}")
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"REQUEST_TIMEOUT: TEFAS metrics request timed out: {err}"],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )
        except Exception as err:
            logger.error(f"TEFAS metrics network error for {clean_symbol}: {err}")
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"NETWORK_ERROR: TEFAS metrics connection failed: {err}"],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        # 6. HTTP Status Code Handling
        if http_status == 429:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["RATE_LIMITED: TEFAS HTTP 429 Too Many Requests."],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )
        elif http_status == 403:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["ACCESS_BLOCKED: TEFAS HTTP 403 Forbidden."],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )
        elif http_status != 200:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=retrieved_at,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"UPSTREAM_ERROR: TEFAS returned HTTP {http_status}."],
                canonical_instrument_id=canonical_id,
                provider_symbol=clean_symbol,
            )

        # 7. Response Parsing
        snapshot = self.parse_current_metrics(
            raw_json_or_text=raw_text,
            provider_symbol=clean_symbol,
            retrieved_at=retrieved_at,
            http_status=http_status,
            resolver=resolver,
            canonical_instrument_id=canonical_id,
        )

        # 8. DataStatus Aggregation
        obs = snapshot.observation
        warnings: List[str] = list(snapshot.diagnostics)
        if obs:
            warnings.extend(obs.diagnostics)

        if not obs or not obs.is_valid:
            status = DataStatus.UNAVAILABLE
        elif (
            obs.investor_count is not None
            and obs.outstanding_units is not None
            and obs.outstanding_units.is_finite()
            and obs.outstanding_units >= Decimal("0")
        ):
            status = DataStatus.COMPLETE
        else:
            status = DataStatus.PARTIAL

        source_meta: Dict[str, Any] = {
            "has_portfolio_size": obs.portfolio_size is not None if obs else False,
            "has_investor_count": obs.investor_count is not None if obs else False,
            "has_outstanding_units": obs.outstanding_units is not None if obs else False,
            "has_reported_current_unit_price": obs.reported_current_unit_price is not None if obs else False,
            "reconciliation_absolute_difference": str(snapshot.reconciliation_absolute_diff) if snapshot.reconciliation_absolute_diff is not None else None,
            "reconciliation_relative_difference": str(snapshot.reconciliation_relative_diff) if snapshot.reconciliation_relative_diff is not None else None,
        }

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=retrieved_at,
            published_at=None,
            effective_date=None,
            status=status,
            raw=snapshot,
            warnings=warnings,
            canonical_instrument_id=canonical_id,
            provider_symbol=clean_symbol,
            source_metadata=source_meta,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """Maps raw snapshot or observation to a canonical field dict."""
        if isinstance(raw, TefasFundMetricsSnapshot):
            return raw.to_dict()
        if isinstance(raw, TefasFundCurrentMetricsObservation):
            return raw.to_dict()
        if isinstance(raw, dict):
            return raw
        return {"raw": str(raw)}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        """Returns warnings for anomalies or missing fields."""
        warnings: List[str] = []
        if "portfolio_size" in normalized and normalized["portfolio_size"] is None:
            warnings.append("Missing portfolio size.")
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
            endpoint="FUND_CURRENT_METRICS",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=response.source_metadata,
        )
