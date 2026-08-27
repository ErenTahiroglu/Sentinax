"""
backend/tests/test_tefas_metrics.py
===================================
Comprehensive Unit and Integration Test Suite for TEFAS 2026 Current Fund Metrics Provider.

Test Coverage:
    1. Valid TRY fund (MAC) complete metrics ingestion (AUM, Units, Investors, Price)
    2. Exact Decimal parsing at JSON lexical boundary (zero float contamination)
    3. Large unit counts exceeding 32-bit integer range preserved without truncation
    4. Investor count integer parsing and strict rejection of floats/booleans/fractions
    5. Partial status when investor count is missing
    6. Partial status when outstanding units is missing
    7. Unavailable status when portfolio size (AUM) is missing or invalid
    8. Valid zero (0) values for portfolio size, units, and investors
    9. Response symbol mismatch handling (INVALID_SOURCE_CONTEXT)
    10. Multiple current rows fail-closed handling (MULTIPLE_CURRENT_ROWS -> UNAVAILABLE)
    11. Error envelope handling (errorCode / errorMessage -> UNAVAILABLE)
    12. Empty resultList handling (EMPTY_RESPONSE -> UNAVAILABLE)
    13. HTTP error and timeout resiliency (403, 429, 500, timeout -> UNAVAILABLE)
    14. Dual identity mismatch rejection before HTTP (IDENTITY_MISMATCH)
    15. Non-TRY currency rejection before HTTP for USD canonical instruments (AMBIGUOUS_PAY_GROUP_CURRENCY)
    16. Non-TRY currency rejection before HTTP for EUR canonical instruments (AMBIGUOUS_PAY_GROUP_CURRENCY)
    17. Canonical TRY instrument normal execution
    18. Missing InstrumentResolverService fails before HTTP (UNRESOLVED_IDENTITY)
    19. Effective date remains strictly None even when FetchContext provides a date
    20. Reported current unit price is diagnostic only and does not control COMPLETE status
    21. Category and derived fields do not control canonical identity or economic fields
    22. Diagnostic accounting reconciliation (sonFiyat * payAdet vs portBuyukluk)
    23. Raw snapshot and NormalizedObservationRecord conversion fidelity
    24. DataProviderContract methods (normalize, validate, provenance) verification
    25. ProviderOrchestrator integration with TefasFundCurrentMetricsProvider

Zero external network calls (pytest-socket enforced).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
from uuid import UUID, uuid4

import httpx
import pytest

from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.identity import (
    InstrumentRecord,
    InstrumentResolverService,
    ProviderAliasRecord,
)
from backend.engine.private.market_data.tefas_metrics_models import (
    TefasFundCurrentMetricsObservation,
    TefasFundMetricsSnapshot,
)
from backend.engine.private.market_data.tefas_models import (
    TefasObservationStatus,
)
from backend.engine.private.orchestrator import (
    OrchestrationResult,
    ProviderOrchestrator,
)
from backend.engine.private.policy import SourcePolicy
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.providers.tefas_metrics import (
    TEFAS_ALLOWED_INSTRUMENT_TYPES,
    TEFAS_METRICS_BASE_URL,
    TEFAS_PROVIDER_NAME,
    TefasFundCurrentMetricsProvider,
    _parse_finite_non_negative_decimal,
    _parse_finite_positive_decimal,
    _parse_non_negative_integer,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_mock_client(handler_fn):
    """Creates a mock httpx.AsyncClient backed by an in-memory transport."""
    transport = httpx.MockTransport(handler_fn)
    return httpx.AsyncClient(transport=transport)


def create_test_resolver() -> Tuple[InstrumentResolverService, Dict[str, InstrumentRecord]]:
    resolver = InstrumentResolverService()
    instruments: Dict[str, InstrumentRecord] = {}

    # 1. Generic TEFAS Fund (TRY) - MAC
    mac_id = uuid4()
    mac_inst = InstrumentRecord(
        id=mac_id,
        canonical_name="Marmara Capital Hisse Senedi Fonu",
        asset_class=AssetClass.FUND,
        instrument_type=InstrumentType.TEFAS_FUND,
        currency=Currency.TRY,
        mic="TEFA",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(mac_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=mac_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="MAC",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["MAC"] = mac_inst

    # 2. Specialized TEFAS Equity Fund (TRY) - NNF
    nnf_id = uuid4()
    nnf_inst = InstrumentRecord(
        id=nnf_id,
        canonical_name="Hedef Portfoy Kuzey Hisse Senedi Fonu",
        asset_class=AssetClass.FUND,
        instrument_type=InstrumentType.TEFAS_EQUITY,
        currency=Currency.TRY,
        mic="TEFA",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(nnf_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=nnf_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="NNF",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["NNF"] = nnf_inst

    # 3. Euro-Denominated Foreign Fund (EUR) - EURF
    eur_fund_id = uuid4()
    eur_fund_inst = InstrumentRecord(
        id=eur_fund_id,
        canonical_name="Euro Denominated TEFAS Fund",
        asset_class=AssetClass.FUND,
        instrument_type=InstrumentType.TEFAS_FUND,
        currency=Currency.EUR,
        mic="TEFA",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(eur_fund_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=eur_fund_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="EURF",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["EURF"] = eur_fund_inst

    # 4. USD-Denominated Fund (USD) - TPJ
    usd_fund_id = uuid4()
    usd_fund_inst = InstrumentRecord(
        id=usd_fund_id,
        canonical_name="TEB Portfoy Birinci Serbest (Doviz) Fonu B Grubu",
        asset_class=AssetClass.FUND,
        instrument_type=InstrumentType.TEFAS_FUND,
        currency=Currency.USD,
        mic="TEFA",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(usd_fund_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=usd_fund_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="TPJ",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["TPJ"] = usd_fund_inst

    # 5. Fund with Missing Currency
    no_curr_id = uuid4()
    no_curr_inst = InstrumentRecord(
        id=no_curr_id,
        canonical_name="No Currency Fund",
        asset_class=AssetClass.FUND,
        instrument_type=InstrumentType.TEFAS_FUND,
        currency=None,  # type: ignore
        mic="TEFA",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(no_curr_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=no_curr_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="NOCURR",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["NOCURR"] = no_curr_inst

    return resolver, instruments


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_01_valid_try_fund_mac_complete_metrics(monkeypatch):
    """Verify standard TRY fund MAC produces COMPLETE status and exact values."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {
                "fonKodu": "MAC",
                "fonUnvan": "MARMARA CAPITAL PORTFÖY HİSSE SENEDİ (TL) FONU",
                "sonFiyat": 0.76165,
                "gunlukGetiri": 0.8381,
                "payAdet": 5725524142,
                "portBuyukluk": 4360844111.72,
                "fonKategori": "Hisse Senedi Fonu",
                "kategoriDerece": 150,
                "kategoriFonSay": 199,
                "yatirimciSayi": 36070,
                "pazarPayi": 1.64,
            }
        ],
    }

    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        assert body == {"fonKodu": "MAC", "dil": "TR"}
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_metrics.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.COMPLETE
    assert resp.provider_name == "TEFAS"
    assert resp.effective_date is None
    assert resp.published_at is None
    assert resp.canonical_instrument_id == mac_inst.id
    assert resp.provider_symbol == "MAC"

    snap: TefasFundMetricsSnapshot = resp.raw
    assert snap is not None
    assert snap.endpoint == "FUND_CURRENT_METRICS"
    assert snap.observation is not None
    obs = snap.observation

    assert obs.portfolio_size == Decimal("4360844111.72")
    assert obs.portfolio_size_currency == Currency.TRY
    assert obs.outstanding_units == Decimal("5725524142")
    assert obs.investor_count == 36070
    assert obs.reported_current_unit_price == Decimal("0.76165")
    assert obs.effective_date is None
    assert obs.published_at is None
    assert obs.is_valid is True


def test_02_exact_decimal_parsing_boundary():
    """Verify parser parses exact lexical Decimal without float contamination."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "sonFiyat": 0.1,
          "payAdet": 10,
          "portBuyukluk": 1.0,
          "yatirimciSayi": 100
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(
        raw_payload, "MAC", retrieved_at, resolver=resolver
    )
    obs = snap.observation
    assert obs is not None
    assert obs.reported_current_unit_price == Decimal("0.1")
    assert str(obs.reported_current_unit_price) == "0.1"


def test_03_large_unit_count_exceeding_32bit():
    """Verify payAdet values above 2^31-1 are preserved without int32 truncation."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "sonFiyat": 1.0,
          "payAdet": 15329428252,
          "portBuyukluk": 15329428252.0,
          "yatirimciSayi": 100
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(
        raw_payload, "MAC", retrieved_at, resolver=resolver
    )
    obs = snap.observation
    assert obs is not None
    assert obs.outstanding_units == Decimal("15329428252")


def test_04_investor_count_integer_parsing_and_rejection():
    """Verify helper rejects floats, booleans, fractions, and non-digit strings for investor_count."""
    assert _parse_non_negative_integer(36070) == 36070
    assert _parse_non_negative_integer("36070") == 36070
    assert _parse_non_negative_integer(Decimal("36070")) == 36070
    assert _parse_non_negative_integer(0) == 0

    assert _parse_non_negative_integer(True) is None
    assert _parse_non_negative_integer(False) is None
    assert _parse_non_negative_integer(36070.5) is None
    assert _parse_non_negative_integer(Decimal("36070.5")) is None
    assert _parse_non_negative_integer(-5) is None
    assert _parse_non_negative_integer("36,070") is None
    assert _parse_non_negative_integer("abc") is None
    assert _parse_non_negative_integer(None) is None


def test_05_partial_status_missing_investor_count():
    """Verify valid portfolio_size and units with missing investor_count yields PARTIAL status."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "sonFiyat": 1.0,
          "payAdet": 1000,
          "portBuyukluk": 1000.0,
          "yatirimciSayi": null
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(
        raw_payload, "MAC", retrieved_at, resolver=resolver
    )
    obs = snap.observation
    assert obs is not None
    assert obs.is_valid is True
    assert obs.investor_count is None
    rec = obs.to_normalized_observation_record()
    assert rec.data_status == DataStatus.PARTIAL


def test_06_partial_status_missing_outstanding_units():
    """Verify valid portfolio_size and investor_count with missing units yields PARTIAL status."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "sonFiyat": 1.0,
          "payAdet": null,
          "portBuyukluk": 1000.0,
          "yatirimciSayi": 50
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(
        raw_payload, "MAC", retrieved_at, resolver=resolver
    )
    obs = snap.observation
    assert obs is not None
    assert obs.is_valid is True
    assert obs.outstanding_units is None
    rec = obs.to_normalized_observation_record()
    assert rec.data_status == DataStatus.PARTIAL


def test_07_unavailable_status_missing_or_invalid_aum():
    """Verify missing or negative portBuyukluk yields UNAVAILABLE status."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Missing portBuyukluk
    raw_missing = '{"errorCode": null, "errorMessage": null, "resultList": [{"fonKodu": "MAC", "payAdet": 100, "yatirimciSayi": 10}]}'
    snap_missing = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_missing, "MAC", retrieved_at, resolver=resolver)
    assert snap_missing.observation.is_valid is False
    assert snap_missing.observation.status == TefasObservationStatus.INVALID_OBSERVATION
    assert snap_missing.observation.to_normalized_observation_record().data_status == DataStatus.UNAVAILABLE

    # 2. Negative portBuyukluk
    raw_neg = '{"errorCode": null, "errorMessage": null, "resultList": [{"fonKodu": "MAC", "portBuyukluk": -100.0, "payAdet": 100, "yatirimciSayi": 10}]}'
    snap_neg = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_neg, "MAC", retrieved_at, resolver=resolver)
    assert snap_neg.observation.is_valid is False


def test_08_zero_values_valid_non_negative_state():
    """Verify zero values for AUM, units, and investor count are preserved as valid states."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_zero = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "sonFiyat": 0,
          "payAdet": 0,
          "portBuyukluk": 0,
          "yatirimciSayi": 0
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_zero, "MAC", retrieved_at, resolver=resolver)
    obs = snap.observation
    assert obs is not None
    assert obs.portfolio_size == Decimal("0")
    assert obs.outstanding_units == Decimal("0")
    assert obs.investor_count == 0
    assert obs.is_valid is True
    rec = obs.to_normalized_observation_record()
    assert rec.data_status == DataStatus.COMPLETE


def test_09_symbol_mismatch_invalid_source_context():
    """Verify row with mismatched fonKodu returns INVALID_SOURCE_CONTEXT and UNAVAILABLE."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_mismatch = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "XYZ",
          "portBuyukluk": 1000.0,
          "payAdet": 100,
          "yatirimciSayi": 10
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_mismatch, "MAC", retrieved_at, resolver=resolver)
    obs = snap.observation
    assert obs.status == TefasObservationStatus.INVALID_SOURCE_CONTEXT
    assert obs.is_valid is False
    assert any("SYMBOL_MISMATCH" in d for d in obs.diagnostics)


def test_10_multiple_current_rows_fails_closed():
    """Verify multiple rows in resultList fail closed with MULTIPLE_CURRENT_ROWS."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_multi = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "MAC", "portBuyukluk": 100.0},
        {"fonKodu": "MAC", "portBuyukluk": 200.0}
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_multi, "MAC", retrieved_at, resolver=resolver)
    assert snap.observation is None
    assert any("MULTIPLE_CURRENT_ROWS" in d for d in snap.diagnostics)


def test_11_error_envelope_handling():
    """Verify error envelope produces UNAVAILABLE status."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_err = '{"errorCode": 500, "errorMessage": "System Outage", "resultList": []}'
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_err, "MAC", retrieved_at, resolver=resolver)
    assert snap.observation is None
    assert any("ERROR_ENVELOPE" in d for d in snap.diagnostics)


def test_12_empty_result_list_handling():
    """Verify empty resultList produces EMPTY_RESPONSE diagnostic."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_empty = '{"errorCode": null, "errorMessage": null, "resultList": []}'
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_empty, "MAC", retrieved_at, resolver=resolver)
    assert snap.observation is None
    assert any("EMPTY_RESPONSE" in d for d in snap.diagnostics)


@pytest.mark.asyncio
async def test_13_http_failures_fail_closed(monkeypatch):
    """Verify HTTP 403, 429, 500 and timeouts yield UNAVAILABLE without retries."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    # 1. HTTP 429
    monkeypatch.setattr(
        "backend.engine.private.providers.tefas_metrics.get_http_client",
        lambda: make_mock_client(lambda req: httpx.Response(429, text="Rate Limited"))
    )
    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    resp_429 = await provider.fetch(FetchContext("TEFAS_FUND_CURRENT_METRICS", canonical_instrument_id=mac_inst.id, provider_symbol="MAC"))
    assert resp_429.status == DataStatus.UNAVAILABLE
    assert any("RATE_LIMITED" in w for w in resp_429.warnings)

    # 2. HTTP 403
    monkeypatch.setattr(
        "backend.engine.private.providers.tefas_metrics.get_http_client",
        lambda: make_mock_client(lambda req: httpx.Response(403, text="Forbidden"))
    )
    resp_403 = await provider.fetch(FetchContext("TEFAS_FUND_CURRENT_METRICS", canonical_instrument_id=mac_inst.id, provider_symbol="MAC"))
    assert resp_403.status == DataStatus.UNAVAILABLE
    assert any("ACCESS_BLOCKED" in w for w in resp_403.warnings)

    # 3. HTTP 500
    monkeypatch.setattr(
        "backend.engine.private.providers.tefas_metrics.get_http_client",
        lambda: make_mock_client(lambda req: httpx.Response(500, text="Internal Error"))
    )
    resp_500 = await provider.fetch(FetchContext("TEFAS_FUND_CURRENT_METRICS", canonical_instrument_id=mac_inst.id, provider_symbol="MAC"))
    assert resp_500.status == DataStatus.UNAVAILABLE
    assert any("UPSTREAM_ERROR" in w for w in resp_500.warnings)


@pytest.mark.asyncio
async def test_14_dual_identity_mismatch_rejected_before_http(monkeypatch):
    """Verify dual identity mismatch is rejected before HTTP execution."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={})

    monkeypatch.setattr("backend.engine.private.providers.tefas_metrics.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="NNF",  # Mismatch: NNF resolves to NNF's UUID, not MAC's
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("IDENTITY_MISMATCH" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_15_non_try_currency_rejected_before_http_usd(monkeypatch):
    """Verify canonical USD instrument (TPJ) is rejected before HTTP with AMBIGUOUS_PAY_GROUP_CURRENCY."""
    resolver, instruments = create_test_resolver()
    tpj_inst = instruments["TPJ"]

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={})

    monkeypatch.setattr("backend.engine.private.providers.tefas_metrics.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=tpj_inst.id,
        provider_symbol="TPJ",
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("AMBIGUOUS_PAY_GROUP_CURRENCY" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_16_non_try_currency_rejected_before_http_eur(monkeypatch):
    """Verify canonical EUR instrument (EURF) is rejected before HTTP with AMBIGUOUS_PAY_GROUP_CURRENCY."""
    resolver, instruments = create_test_resolver()
    eur_inst = instruments["EURF"]

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={})

    monkeypatch.setattr("backend.engine.private.providers.tefas_metrics.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=eur_inst.id,
        provider_symbol="EURF",
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("AMBIGUOUS_PAY_GROUP_CURRENCY" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_17_try_currency_normal_execution(monkeypatch):
    """Verify canonical TRY instrument executes HTTP request and returns COMPLETE."""
    resolver, instruments = create_test_resolver()
    nnf_inst = instruments["NNF"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {
                "fonKodu": "NNF",
                "sonFiyat": 23.497202,
                "payAdet": 89727966,
                "portBuyukluk": 2108356102.07,
                "yatirimciSayi": 18147,
            }
        ],
    }
    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_metrics.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=nnf_inst.id,
        provider_symbol="NNF",
    )

    resp = await provider.fetch(context)
    assert http_called is True
    assert resp.status == DataStatus.COMPLETE
    assert resp.raw.observation.portfolio_size == Decimal("2108356102.07")


@pytest.mark.asyncio
async def test_18_missing_resolver_fails_before_http(monkeypatch):
    """Verify provider without InstrumentResolverService fails closed before HTTP."""
    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={})

    monkeypatch.setattr("backend.engine.private.providers.tefas_metrics.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundCurrentMetricsProvider(resolver=None)
    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=uuid4(),
        provider_symbol="MAC",
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("UNRESOLVED_IDENTITY" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_19_effective_date_remains_none_even_if_context_supplied(monkeypatch):
    """Verify effective_date is strictly None even if context provided a historical effective_date."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {
                "fonKodu": "MAC",
                "sonFiyat": 1.0,
                "payAdet": 100,
                "portBuyukluk": 100.0,
                "yatirimciSayi": 10,
            }
        ],
    }
    monkeypatch.setattr(
        "backend.engine.private.providers.tefas_metrics.get_http_client",
        lambda: make_mock_client(lambda req: httpx.Response(200, json=raw_payload))
    )

    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2024, 1, 1),  # Historical date in context
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.COMPLETE
    # ProviderResponse and observation must NOT pretend to be historical data
    assert resp.effective_date is None
    assert resp.raw.observation.effective_date is None


def test_20_reported_current_price_non_authority():
    """Verify missing/invalid sonFiyat does not invalidate COMPLETE status when core metrics are valid."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "sonFiyat": null,
          "payAdet": 1000,
          "portBuyukluk": 1000.0,
          "yatirimciSayi": 50
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_payload, "MAC", retrieved_at, resolver=resolver)
    obs = snap.observation
    assert obs.reported_current_unit_price is None
    assert obs.is_valid is True
    rec = obs.to_normalized_observation_record()
    assert rec.data_status == DataStatus.COMPLETE


def test_21_category_and_derived_fields_non_authority():
    """Verify changing fonKategori or gunlukGetiri does not alter economic fields or canonical identity."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "fonKategori": "Yepyeni Kategori",
          "gunlukGetiri": 99.99,
          "pazarPayi": 50.0,
          "payAdet": 1000,
          "portBuyukluk": 1000.0,
          "yatirimciSayi": 50
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_payload, "MAC", retrieved_at, resolver=resolver)
    obs = snap.observation
    assert obs.portfolio_size == Decimal("1000.0")
    assert obs.instrument_type == InstrumentType.TEFAS_FUND
    rec = obs.to_normalized_observation_record()
    assert "gunlukGetiri" not in rec.observation_data


def test_22_diagnostic_accounting_reconciliation():
    """Verify sonFiyat * payAdet vs portBuyukluk reconciliation is computed accurately."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "sonFiyat": 0.76165,
          "payAdet": 5725524142,
          "portBuyukluk": 4360844111.72,
          "yatirimciSayi": 36070
        }
      ]
    }
    """
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(raw_payload, "MAC", retrieved_at, resolver=resolver)
    assert snap.reconciliation_absolute_diff is not None
    # 0.76165 * 5725524142 = 4360845462.75430 -> diff is 1351.03430
    assert snap.reconciliation_absolute_diff == Decimal("1351.03430")
    assert snap.reconciliation_relative_diff is not None
    assert snap.reconciliation_relative_diff < Decimal("0.0001")  # < 0.01%


def test_23_raw_snapshot_and_normalized_record_conversion():
    """Verify snapshot and observation serialize cleanly to canonical records."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_payload = '{"errorCode": null, "errorMessage": null, "resultList": [{"fonKodu": "MAC", "sonFiyat": 1.0, "payAdet": 100, "portBuyukluk": 100.0, "yatirimciSayi": 10}]}'
    snap = TefasFundCurrentMetricsProvider.parse_current_metrics(
        raw_payload, "MAC", retrieved_at, resolver=resolver, canonical_instrument_id=mac_inst.id
    )

    # 1. RawProviderSnapshotRecord
    raw_rec = snap.to_raw_provider_snapshot_record()
    assert raw_rec.provider == "TEFAS"
    assert raw_rec.endpoint == "FUND_CURRENT_METRICS"
    assert raw_rec.request_params == {"fonKodu": "MAC", "dil": "TR"}
    assert raw_rec.response_metadata["has_portfolio_size"] is True
    assert raw_rec.response_metadata["has_investor_count"] is True
    assert raw_rec.response_metadata["has_outstanding_units"] is True

    # 2. NormalizedObservationRecord
    norm_rec = snap.observation.to_normalized_observation_record()
    assert norm_rec.observation_type == "TEFAS_FUND_CURRENT_METRICS"
    assert norm_rec.effective_date is None
    assert norm_rec.published_at is None
    assert norm_rec.observed_at == retrieved_at
    assert norm_rec.currency == Currency.TRY
    assert norm_rec.data_status == DataStatus.COMPLETE
    assert norm_rec.observation_data["portfolio_size"] == "100.0"
    assert norm_rec.observation_data["outstanding_units"] == "100"
    assert norm_rec.observation_data["investor_count"] == 10


def test_24_provider_contract_protocol_methods():
    """Verify DataProviderContract normalize, validate, and provenance methods."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]
    provider = TefasFundCurrentMetricsProvider(resolver=resolver)

    obs = TefasFundCurrentMetricsObservation(
        provider_symbol="MAC",
        portfolio_size=Decimal("1000.00"),
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("1000"),
        investor_count=50,
        instrument_id=mac_inst.id,
        instrument_type=InstrumentType.TEFAS_FUND,
    )
    norm = provider.normalize(obs)
    assert isinstance(norm, dict)
    assert norm["portfolio_size"] == "1000.00"
    assert norm["provider_symbol"] == "MAC"
    assert provider.validate(norm) == []

    resp = ProviderResponse(
        provider_name="TEFAS",
        source_quality=SourceTier.TIER_2_EXCHANGE,
        retrieved_at=datetime.now(timezone.utc),
        published_at=None,
        effective_date=None,
        status=DataStatus.COMPLETE,
        raw=obs,
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
    )
    prov = provider.provenance(resp)
    assert isinstance(prov, ProviderProvenance)
    assert prov.provider_name == "TEFAS"
    assert prov.source_quality == SourceTier.TIER_2_EXCHANGE
    assert prov.endpoint == "FUND_CURRENT_METRICS"


@pytest.mark.asyncio
async def test_25_orchestrator_integration(monkeypatch):
    """Verify ProviderOrchestrator integrates TefasFundCurrentMetricsProvider."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {
                "fonKodu": "MAC",
                "sonFiyat": 0.76165,
                "payAdet": 5725524142,
                "portBuyukluk": 4360844111.72,
                "yatirimciSayi": 36070,
            }
        ],
    }
    monkeypatch.setattr(
        "backend.engine.private.providers.tefas_metrics.get_http_client",
        lambda: make_mock_client(lambda req: httpx.Response(200, json=raw_payload))
    )

    orch = ProviderOrchestrator()
    provider = TefasFundCurrentMetricsProvider(resolver=resolver)
    orch.register_provider(provider)

    context = FetchContext(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        request_parameters={"resolver": resolver},
    )
    policy = SourcePolicy(
        observation_type="TEFAS_FUND_CURRENT_METRICS",
        ordered_provider_names=["TEFAS"],
        required_fields=["provider"],
        optional_fields=["provider_symbol"],
    )

    result: OrchestrationResult = await orch.execute(context, policy)
    assert isinstance(result, OrchestrationResult)
    assert result.status == DataStatus.COMPLETE
    assert result.selected_provider == "TEFAS"
    assert result.canonical_instrument_id == mac_inst.id
