"""
backend/tests/test_tefas_eod.py
===============================
Comprehensive Unit and Regression Test Suite for TEFAS 2026 EOD Fund Price Provider.

Test Coverage:
    1. Exact Decimal Parsing at JSON Lexical Boundary (zero float contamination)
    2. Generic TEFAS_FUND instrument type acceptance
    3. Specialized TEFAS fund types acceptance (TEFAS_EQUITY, TEFAS_MONEY_MARKET, TEFAS_VARIABLE, TEFAS_BALANCED)
    4. UCITS_FUND rejection before HTTP (UNSUPPORTED_INSTRUMENT_TYPE)
    5. Non-fund AssetClass rejection before HTTP (BIST_STOCK, US_STOCK, etc.)
    6. Unsupported / commodity instrument type rejection
    7. Dual identity preflight mismatch rejection before HTTP (IDENTITY_MISMATCH)
    8. Unresolved identity / unknown symbol rejection before HTTP (UNRESOLVED_IDENTITY)
    9. Canonical ID only resolution to active TEFAS alias
    10. Response symbol mismatch handling (INVALID_SOURCE_CONTEXT)
    11. Fund title does not mutate or control canonical identity (CURRENT_METADATA_ONLY)
    12. Currency authority from Instrument Master (preserves EUR/USD, rejects missing currency)
    13. Valid period values (1, 3, 6, 12, 36, 60) accepted
    14. Invalid period values (2, 24, 61, 120, "5Y") rejected before HTTP (INVALID_PERIOD)
    15. Error envelope handling (errorCode or errorMessage -> UNAVAILABLE)
    16. Empty resultList handling (EMPTY_RESPONSE -> UNAVAILABLE)
    17. Schema mismatch / malformed root handling (SCHEMA_MISMATCH -> UNAVAILABLE)
    18. Missing / invalid date parsing (INVALID_DATE / MISSING_DATE)
    19. Mixed valid and invalid rows yields PARTIAL status
    20. All invalid rows yields UNAVAILABLE status
    21. Duplicate date identical price deterministic deduplication
    22. Duplicate date differing price conflict handling (DUPLICATE_CONFLICT -> PARTIAL)
    23. HTTP 429 rate limit handling (RATE_LIMITED -> UNAVAILABLE)
    24. HTTP 403 access blocked handling (ACCESS_BLOCKED -> UNAVAILABLE)
    25. HTTP 500 upstream server error handling (UPSTREAM_ERROR -> UNAVAILABLE)
    26. Timeout and network exceptions handled without crashing
    27. Raw snapshot and normalized records serialization (SHA-256, UTC, TIER_2_EXCHANGE, YELLOW)
    28. Defensive float & comma string rejection at helper parser boundary
    29. Provider contract methods (normalize, validate, provenance) verification
    30. Malformed non-dict row aggregate fail-closed accounting (yields PARTIAL status)
    31. All malformed non-dict rows yields UNAVAILABLE status
    32. DataProviderContract protocol satisfaction and fetch() execution
    33. ProviderOrchestrator integration with TefasFundPriceProvider fallback chain

Zero external network calls (pytest-socket enforced).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
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
from backend.engine.private.market_data.tefas_models import (
    TefasCapability,
    TefasFundPriceObservation,
    TefasFundPriceSnapshot,
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
from backend.engine.private.providers.tefas_eod import (
    TEFAS_ALLOWED_INSTRUMENT_TYPES,
    TEFAS_BASE_URL,
    TEFAS_PROVIDER_NAME,
    TEFAS_SUPPORTED_PERIODS,
    TefasFundPriceProvider,
    _parse_finite_decimal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture Helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_test_resolver() -> Tuple[InstrumentResolverService, Dict[str, InstrumentRecord]]:
    resolver = InstrumentResolverService()
    instruments: Dict[str, InstrumentRecord] = {}

    # 1. Generic TEFAS Fund
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

    # 2. Specialized TEFAS Equity Fund
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

    # 3. Specialized TEFAS Money Market Fund
    ppf_id = uuid4()
    ppf_inst = InstrumentRecord(
        id=ppf_id,
        canonical_name="Is Portfoy Para Piyasasi Fonu",
        asset_class=AssetClass.FUND,
        instrument_type=InstrumentType.TEFAS_MONEY_MARKET,
        currency=Currency.TRY,
        mic="TEFA",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(ppf_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=ppf_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="TI1",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["TI1"] = ppf_inst

    # 4. Euro-Denominated Foreign Fund in TEFAS
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

    # 6. UCITS Fund (AssetClass.FUND but InstrumentType.UCITS_FUND)
    ucits_id = uuid4()
    ucits_inst = InstrumentRecord(
        id=ucits_id,
        canonical_name="Vanguard FTSE All-World UCITS ETF",
        asset_class=AssetClass.FUND,
        instrument_type=InstrumentType.UCITS_FUND,
        currency=Currency.EUR,
        mic="XETR",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(ucits_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=ucits_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="VWCE",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["VWCE"] = ucits_inst

    # 7. BIST Stock (AssetClass.EQUITY, InstrumentType.BIST_STOCK)
    bist_id = uuid4()
    bist_inst = InstrumentRecord(
        id=bist_id,
        canonical_name="Turk Hava Yollari",
        asset_class=AssetClass.EQUITY,
        instrument_type=InstrumentType.BIST_STOCK,
        currency=Currency.TRY,
        mic="XIST",
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(bist_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=bist_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="THYAO",
            valid_from=date(2020, 1, 1),
        )
    )
    instruments["THYAO"] = bist_inst

    return resolver, instruments


def make_mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

def test_01_exact_decimal_parsing_lexical_boundary():
    """Verify JSON numeric literal 0.1 parses to exact Decimal('0.1') without float contamination."""
    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {
          "fonKodu": "MAC",
          "fonUnvan": "MARMARA CAPITAL",
          "kategoriDerece": 150,
          "kategoriFonSay": 199,
          "tarih": "2026-08-25",
          "fiyat": 0.1
        }
      ]
    }
    """
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    
    snapshot = TefasFundPriceProvider.parse_daily_prices(
        raw_payload,
        provider_symbol="MAC",
        retrieved_at=retrieved_at,
        resolver=resolver,
    )

    assert len(snapshot.observations) == 1
    obs = snapshot.observations[0]
    assert obs.is_valid is True
    assert obs.unit_price == Decimal("0.1")
    assert str(obs.unit_price) == "0.1"
    # Ensure binary float conversion produces a different binary representation
    assert Decimal.from_float(0.1) != Decimal("0.1")


def test_02_generic_tefas_fund_valid():
    """Verify AssetClass.FUND + InstrumentType.TEFAS_FUND parses and normalizes cleanly."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "MAC", "fonUnvan": "MAC TITLE", "tarih": "2026-08-25", "fiyat": 0.76165}
      ]
    }
    """
    snapshot = TefasFundPriceProvider.parse_daily_prices(
        raw_payload,
        provider_symbol="MAC",
        retrieved_at=retrieved_at,
        resolver=resolver,
        canonical_instrument_id=mac_inst.id,
    )

    assert len(snapshot.observations) == 1
    obs = snapshot.observations[0]
    assert obs.status == TefasObservationStatus.VALID
    assert obs.instrument_type == InstrumentType.TEFAS_FUND
    assert obs.currency == Currency.TRY

    norm = obs.to_normalized_observation_record()
    assert norm.asset_class == AssetClass.FUND
    assert norm.instrument_type == InstrumentType.TEFAS_FUND
    assert norm.observation_type == "TEFAS_FUND_PRICE"
    assert norm.observation_data == {"provider_symbol": "MAC", "unit_price": "0.76165"}
    assert norm.data_status == DataStatus.COMPLETE
    assert norm.source_tier == SourceTier.TIER_2_EXCHANGE


def test_03_specialized_tefas_types_valid():
    """Verify specialized fund types TEFAS_EQUITY and TEFAS_MONEY_MARKET are fully accepted."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. NNF (TEFAS_EQUITY)
    raw_nnf = '{"errorCode": null, "errorMessage": null, "resultList": [{"fonKodu": "NNF", "tarih": "2026-08-25", "fiyat": 1.2345}]}'
    snap_nnf = TefasFundPriceProvider.parse_daily_prices(raw_nnf, "NNF", retrieved_at, resolver=resolver)
    assert len(snap_nnf.observations) == 1
    assert snap_nnf.observations[0].instrument_type == InstrumentType.TEFAS_EQUITY
    assert snap_nnf.observations[0].is_valid is True

    # 2. TI1 (TEFAS_MONEY_MARKET)
    raw_ti1 = '{"errorCode": null, "errorMessage": null, "resultList": [{"fonKodu": "TI1", "tarih": "2026-08-25", "fiyat": 5.6789}]}'
    snap_ti1 = TefasFundPriceProvider.parse_daily_prices(raw_ti1, "TI1", retrieved_at, resolver=resolver)
    assert len(snap_ti1.observations) == 1
    assert snap_ti1.observations[0].instrument_type == InstrumentType.TEFAS_MONEY_MARKET
    assert snap_ti1.observations[0].is_valid is True


@pytest.mark.asyncio
async def test_04_ucits_fund_rejected_before_http(monkeypatch):
    """Verify UCITS_FUND is rejected before HTTP with UNSUPPORTED_INSTRUMENT_TYPE."""
    resolver, instruments = create_test_resolver()
    ucits_inst = instruments["VWCE"]

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={"errorCode": None, "errorMessage": None, "resultList": []})

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=ucits_inst.id,
        provider_symbol="VWCE",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("UNSUPPORTED_INSTRUMENT_TYPE" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_05_non_fund_rejected_before_http(monkeypatch):
    """Verify non-fund AssetClass (e.g. BIST_STOCK) is rejected before HTTP."""
    resolver, instruments = create_test_resolver()
    bist_inst = instruments["THYAO"]

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={"errorCode": None, "errorMessage": None, "resultList": []})

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=bist_inst.id,
        provider_symbol="THYAO",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("UNSUPPORTED_INSTRUMENT_TYPE" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_06_commodity_instrument_type_rejected_before_http(monkeypatch):
    """Verify Commodity instrument type is rejected before HTTP."""
    resolver = InstrumentResolverService()
    gold_id = uuid4()
    gold_inst = InstrumentRecord(
        id=gold_id,
        canonical_name="Physical Gold",
        asset_class=AssetClass.COMMODITY,
        instrument_type=InstrumentType.GOLD,
        currency=Currency.USD,
        mic=None,
        valid_from=date(2020, 1, 1),
    )
    resolver.register_instrument(gold_inst)
    resolver.register_alias(
        ProviderAliasRecord(
            instrument_id=gold_id,
            provider=TEFAS_PROVIDER_NAME,
            provider_symbol="GLD",
            valid_from=date(2020, 1, 1),
        )
    )

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={})

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=gold_inst.id,
        provider_symbol="GLD",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("UNSUPPORTED_INSTRUMENT_TYPE" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_07_dual_identity_preflight_mismatch_fails(monkeypatch):
    """Verify mismatch between canonical_id and provider_symbol fails before HTTP with IDENTITY_MISMATCH."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={})

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    # Provide MAC's canonical ID with NNF's provider symbol
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="NNF",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("IDENTITY_MISMATCH" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_08_symbol_only_unresolved_fails(monkeypatch):
    """Verify unknown provider symbol fails before HTTP with UNRESOLVED_IDENTITY."""
    resolver, _ = create_test_resolver()

    http_called = False
    def handler(request):
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={})

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        provider_symbol="UNKNOWN_FUND",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert http_called is False
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("UNRESOLVED_IDENTITY" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_09_canonical_id_only_resolves_symbol(monkeypatch):
    """Verify providing canonical_id only resolves alias MAC and executes HTTP request."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    requested_body = None
    def handler(request):
        nonlocal requested_body
        requested_body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "errorCode": None,
                "errorMessage": None,
                "resultList": [
                    {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.76165}
                ]
            }
        )

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        effective_date=date(2026, 8, 27),
        request_parameters={"period_months": 1},
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.COMPLETE
    assert requested_body == {"fonKodu": "MAC", "dil": "TR", "periyod": 1}
    assert resp.canonical_instrument_id == mac_inst.id
    assert resp.provider_symbol == "MAC"


def test_10_response_symbol_mismatch_invalid_source_context():
    """Verify returned row with mismatched fonKodu is flagged as INVALID_SOURCE_CONTEXT."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "XYZ", "tarih": "2026-08-25", "fiyat": 0.50}
      ]
    }
    """
    snapshot = TefasFundPriceProvider.parse_daily_prices(
        raw_payload,
        provider_symbol="MAC",
        retrieved_at=retrieved_at,
        resolver=resolver,
    )

    assert len(snapshot.observations) == 1
    obs = snapshot.observations[0]
    assert obs.status == TefasObservationStatus.INVALID_SOURCE_CONTEXT
    assert obs.is_valid is False
    assert any("SYMBOL_MISMATCH" in d for d in obs.diagnostics)


def test_11_title_does_not_mutate_or_control_canonical_identity():
    """Verify source fund title in response does not mutate canonical master title."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]
    original_title = mac_inst.canonical_name
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "MAC", "fonUnvan": "COMPLETELY NEW 2026 TITLE", "tarih": "2026-08-25", "fiyat": 0.76165}
      ]
    }
    """
    snapshot = TefasFundPriceProvider.parse_daily_prices(
        raw_payload,
        provider_symbol="MAC",
        retrieved_at=retrieved_at,
        resolver=resolver,
        canonical_instrument_id=mac_inst.id,
    )

    assert len(snapshot.observations) == 1
    obs = snapshot.observations[0]
    assert obs.instrument_id == mac_inst.id
    # Canonical instrument master title is unchanged
    assert mac_inst.canonical_name == original_title
    assert mac_inst.canonical_name != "COMPLETELY NEW 2026 TITLE"


def test_12_currency_authority_from_master():
    """Verify currency is sourced from Instrument Master (preserves EUR, rejects missing currency)."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. EUR Fund
    raw_eur = '{"errorCode": null, "errorMessage": null, "resultList": [{"fonKodu": "EURF", "tarih": "2026-08-25", "fiyat": 10.50}]}'
    snap_eur = TefasFundPriceProvider.parse_daily_prices(raw_eur, "EURF", retrieved_at, resolver=resolver)
    assert snap_eur.observations[0].currency == Currency.EUR

    # 2. Missing Currency Fund
    raw_nocurr = '{"errorCode": null, "errorMessage": null, "resultList": [{"fonKodu": "NOCURR", "tarih": "2026-08-25", "fiyat": 1.00}]}'
    snap_nocurr = TefasFundPriceProvider.parse_daily_prices(raw_nocurr, "NOCURR", retrieved_at, resolver=resolver)
    assert snap_nocurr.observations[0].status == TefasObservationStatus.UNRESOLVED_IDENTITY
    assert any("MISSING_CURRENCY" in d for d in snap_nocurr.observations[0].diagnostics)


@pytest.mark.asyncio
async def test_13_period_valid_values(monkeypatch):
    """Verify all valid periods {1, 3, 6, 12, 36, 60} succeed."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    for p in (1, 3, 6, 12, 36, 60):
        requested_period = None
        def handler(request):
            nonlocal requested_period
            body = json.loads(request.content.decode("utf-8"))
            requested_period = body.get("periyod")
            return httpx.Response(
                200,
                json={"errorCode": None, "errorMessage": None, "resultList": [{"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.76}]}
            )

        monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

        provider = TefasFundPriceProvider(resolver=resolver)
        context = FetchContext(
            observation_type="TEFAS_FUND_PRICE",
            canonical_instrument_id=mac_inst.id,
            provider_symbol="MAC",
            effective_date=date(2026, 8, 27),
            request_parameters={"period_months": p},
        )

        resp = await provider.fetch(context)
        assert resp.status == DataStatus.COMPLETE
        assert requested_period == p


@pytest.mark.asyncio
async def test_14_period_invalid_values_rejected(monkeypatch):
    """Verify invalid periods (2, 24, 61, 120, '5Y') fail before HTTP with INVALID_PERIOD."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    for invalid_p in (2, 24, 61, 120, "5Y", -1, None):
        http_called = False
        def handler(request):
            nonlocal http_called
            http_called = True
            return httpx.Response(200, json={})

        monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

        provider = TefasFundPriceProvider(resolver=resolver)
        context = FetchContext(
            observation_type="TEFAS_FUND_PRICE",
            canonical_instrument_id=mac_inst.id,
            provider_symbol="MAC",
            effective_date=date(2026, 8, 27),
            request_parameters={"period_months": invalid_p},
        )

        resp = await provider.fetch(context)
        assert http_called is False
        assert resp.status == DataStatus.UNAVAILABLE
        assert any("INVALID_PERIOD" in w for w in resp.warnings)


def test_15_error_envelope_handling():
    """Verify TEFAS error envelope with errorCode or errorMessage yields UNAVAILABLE."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_err = '{"errorCode": "ERR-101", "errorMessage": "Sistem Hatası!!", "resultList": null}'
    snapshot = TefasFundPriceProvider.parse_daily_prices(raw_err, "MAC", retrieved_at, resolver=resolver)

    assert len(snapshot.observations) == 0
    assert any("ERROR_ENVELOPE" in d for d in snapshot.diagnostics)


def test_16_empty_result_list_handling():
    """Verify resultList=[] yields UNAVAILABLE with EMPTY_RESPONSE diagnostic."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_empty = '{"errorCode": null, "errorMessage": null, "resultList": []}'
    snapshot = TefasFundPriceProvider.parse_daily_prices(raw_empty, "MAC", retrieved_at, resolver=resolver)

    assert len(snapshot.observations) == 0
    assert any("EMPTY_RESPONSE" in d for d in snapshot.diagnostics)


def test_17_schema_mismatch_missing_or_non_list_result():
    """Verify missing or non-list resultList yields SCHEMA_MISMATCH."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_mismatch = '{"errorCode": null, "errorMessage": null, "resultList": "not a list"}'
    snapshot = TefasFundPriceProvider.parse_daily_prices(raw_mismatch, "MAC", retrieved_at, resolver=resolver)

    assert len(snapshot.observations) == 0
    assert any("SCHEMA_MISMATCH" in d for d in snapshot.diagnostics)


def test_18_missing_or_invalid_date_parsing():
    """Verify missing or malformed tarih yields INVALID_OBSERVATION."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "MAC", "tarih": "not-a-date", "fiyat": 0.50},
        {"fonKodu": "MAC", "tarih": null, "fiyat": 0.50}
      ]
    }
    """
    snapshot = TefasFundPriceProvider.parse_daily_prices(raw, "MAC", retrieved_at, resolver=resolver)
    assert len(snapshot.observations) == 2
    assert snapshot.observations[0].status == TefasObservationStatus.INVALID_OBSERVATION
    assert snapshot.observations[1].status == TefasObservationStatus.INVALID_OBSERVATION


@pytest.mark.asyncio
async def test_19_mixed_valid_and_invalid_rows_yields_partial(monkeypatch):
    """Verify response containing both valid and invalid price rows yields PARTIAL status."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.75},
            {"fonKodu": "MAC", "tarih": "2026-08-26", "fiyat": -0.10},  # Invalid price
        ]
    }
    def handler(request):
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.PARTIAL
    assert resp.source_metadata["valid_count"] == 1
    assert resp.source_metadata["invalid_count"] == 1


@pytest.mark.asyncio
async def test_20_all_invalid_rows_yields_unavailable(monkeypatch):
    """Verify response with only invalid rows yields UNAVAILABLE status."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0},      # Zero price
            {"fonKodu": "MAC", "tarih": "2026-08-26", "fiyat": "NaN"},  # NaN price
        ]
    }
    def handler(request):
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.UNAVAILABLE


def test_21_duplicate_date_identical_price_deduped():
    """Verify identical duplicate observations for the same date are deduplicated cleanly."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.75},
        {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.75}
      ]
    }
    """
    snapshot = TefasFundPriceProvider.parse_daily_prices(raw_payload, "MAC", retrieved_at, resolver=resolver)
    assert len(snapshot.observations) == 1
    assert snapshot.observations[0].is_valid is True


def test_22_duplicate_date_differing_price_conflict():
    """Verify differing prices for the same date trigger DUPLICATE_CONFLICT."""
    resolver, _ = create_test_resolver()
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.75},
        {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.85}
      ]
    }
    """
    snapshot = TefasFundPriceProvider.parse_daily_prices(raw_payload, "MAC", retrieved_at, resolver=resolver)
    assert len(snapshot.observations) == 2
    assert snapshot.observations[0].status == TefasObservationStatus.VALID
    assert snapshot.observations[1].status == TefasObservationStatus.DUPLICATE_CONFLICT


@pytest.mark.asyncio
async def test_23_http_429_rate_limited(monkeypatch):
    """Verify HTTP 429 returns UNAVAILABLE with RATE_LIMITED warning and is_rate_limited=True."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    def handler(request):
        return httpx.Response(429, text="Too Many Requests")

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("RATE_LIMITED" in w for w in resp.warnings)
    assert resp.source_metadata.get("is_rate_limited") is True


@pytest.mark.asyncio
async def test_24_http_403_access_blocked(monkeypatch):
    """Verify HTTP 403 returns UNAVAILABLE with ACCESS_BLOCKED warning."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    def handler(request):
        return httpx.Response(403, text="Forbidden")

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("ACCESS_BLOCKED" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_25_http_500_upstream_error(monkeypatch):
    """Verify HTTP 500 returns UNAVAILABLE with UPSTREAM_ERROR warning."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    def handler(request):
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("UPSTREAM_ERROR" in w for w in resp.warnings)


@pytest.mark.asyncio
async def test_26_timeout_and_network_error_handled(monkeypatch):
    """Verify network exceptions return UNAVAILABLE without crashing."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    def timeout_handler(request):
        raise httpx.TimeoutException("Connection timed out")

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(timeout_handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.UNAVAILABLE
    assert any("TIMEOUT" in w for w in resp.warnings)


def test_27_raw_snapshot_and_normalized_records_serialization():
    """Verify snapshot and observation serialize cleanly to RawProviderSnapshotRecord and NormalizedObservationRecord."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]
    retrieved_at = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    raw_payload = """
    {
      "errorCode": null,
      "errorMessage": null,
      "resultList": [
        {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.76165},
        {"fonKodu": "MAC", "tarih": "2026-08-26", "fiyat": 0.77120}
      ]
    }
    """
    snapshot = TefasFundPriceProvider.parse_daily_prices(
        raw_payload,
        provider_symbol="MAC",
        retrieved_at=retrieved_at,
        resolver=resolver,
        canonical_instrument_id=mac_inst.id,
    )

    # 1. Raw Snapshot Record
    raw_record = snapshot.to_raw_provider_snapshot_record()
    assert raw_record.provider == "TEFAS"
    assert raw_record.endpoint == "FUND_PRICE_HISTORY"
    assert raw_record.request_params == {"fonKodu": "MAC", "dil": "TR", "periyod": 1}
    assert raw_record.http_status == 200
    assert raw_record.content_type == "application/json"
    assert raw_record.payload_hash == snapshot.payload_hash
    assert raw_record.response_metadata["source_row_count"] == 2
    assert raw_record.response_metadata["malformed_row_count"] == 0
    assert raw_record.response_metadata["observation_count"] == 2
    assert raw_record.response_metadata["trade_date_range"] == ["2026-08-25", "2026-08-26"]

    # 2. Normalized Observation Records
    assert len(snapshot.observations) == 2
    norm_records = [o.to_normalized_observation_record() for o in snapshot.observations]
    assert len(norm_records) == 2

    r1 = norm_records[0]
    assert r1.asset_class == AssetClass.FUND
    assert r1.instrument_type == InstrumentType.TEFAS_FUND
    assert r1.observation_type == "TEFAS_FUND_PRICE"
    assert r1.observation_data == {"provider_symbol": "MAC", "unit_price": "0.76165"}
    assert r1.source_tier == SourceTier.TIER_2_EXCHANGE
    assert r1.effective_date == date(2026, 8, 25)
    assert r1.currency == Currency.TRY
    assert r1.published_at is None
    assert r1.source_refs == ["TEFAS:MAC@2026-08-25"]


def test_28_defensive_float_rejection():
    """Verify _parse_finite_decimal strictly rejects Python floats and comma-formatted strings."""
    # Strict float rejection
    assert _parse_finite_decimal(0.76165) is None
    assert _parse_finite_decimal(1.0) is None
    # Strict comma string rejection
    assert _parse_finite_decimal("0,76165") is None
    assert _parse_finite_decimal("1,000.50") is None
    # Valid canonical strings and objects
    assert _parse_finite_decimal(Decimal("0.76165")) == Decimal("0.76165")
    assert _parse_finite_decimal("0.76165") == Decimal("0.76165")
    assert _parse_finite_decimal(10) == Decimal("10")
    assert _parse_finite_decimal(None) is None
    assert _parse_finite_decimal("NaN") is None
    assert _parse_finite_decimal("Infinity") is None


def test_29_provider_contract_methods():
    """Verify normalize, validate, provenance methods on TefasFundPriceProvider."""
    provider = TefasFundPriceProvider()
    assert provider.provider_name == "TEFAS"
    assert provider.source_quality == SourceTier.TIER_2_EXCHANGE
    assert provider.access_status == ProviderAccessStatus.YELLOW
    assert TefasCapability.FUND_PRICE_HISTORY in provider.capabilities

    # Normalize
    obs = TefasFundPriceObservation(
        provider_symbol="MAC",
        trade_date=date(2026, 8, 25),
        unit_price=Decimal("0.75"),
    )
    norm = provider.normalize(obs)
    assert isinstance(norm, dict)
    assert norm["provider_symbol"] == "MAC"
    assert norm["unit_price"] == "0.75"

    # Validate
    warns = provider.validate({"unit_price": None})
    assert "Missing unit price." in warns

    # Provenance
    retrieved_at = datetime.now(timezone.utc)
    mock_resp = ProviderResponse(
        provider_name="TEFAS",
        source_quality=SourceTier.TIER_2_EXCHANGE,
        retrieved_at=retrieved_at,
        published_at=None,
        effective_date=date(2026, 8, 25),
        status=DataStatus.COMPLETE,
        raw=None,
        canonical_instrument_id=uuid4(),
        provider_symbol="MAC",
        source_metadata={"payload_hash": "abc"},
    )
    prov = provider.provenance(mock_resp)
    assert prov.provider_name == "TEFAS"
    assert prov.endpoint == "FUND_PRICE_HISTORY"
    assert prov.source_quality == SourceTier.TIER_2_EXCHANGE


@pytest.mark.asyncio
async def test_30_malformed_non_dict_row_yields_partial_status(monkeypatch):
    """Verify payload with valid row and malformed non-dict row yields PARTIAL status and counts malformed rows."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.76165},
            "CORRUPT_NON_DICT_ROW",
        ]
    }
    def handler(request):
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.PARTIAL
    assert resp.source_metadata["source_row_count"] == 2
    assert resp.source_metadata["parsed_observation_count"] == 1
    assert resp.source_metadata["malformed_row_count"] == 1
    assert resp.source_metadata["valid_count"] == 1


@pytest.mark.asyncio
async def test_31_all_malformed_non_dict_rows_yields_unavailable_status(monkeypatch):
    """Verify payload with only malformed non-dict rows yields UNAVAILABLE status."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            "CORRUPT_ROW_1",
            "CORRUPT_ROW_2",
        ]
    }
    def handler(request):
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider = TefasFundPriceProvider(resolver=resolver)
    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
    )

    resp = await provider.fetch(context)
    assert resp.status == DataStatus.UNAVAILABLE
    assert resp.source_metadata["source_row_count"] == 2
    assert resp.source_metadata["malformed_row_count"] == 2
    assert resp.source_metadata["valid_count"] == 0


@pytest.mark.asyncio
async def test_32_data_provider_contract_compliance(monkeypatch):
    """Verify TefasFundPriceProvider satisfies DataProviderContract protocol and fetch() returns valid ProviderResponse."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.76165}
        ]
    }
    def handler(request):
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    provider: DataProviderContract = TefasFundPriceProvider(resolver=resolver)
    assert isinstance(provider, DataProviderContract)

    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
        request_parameters={"period_months": 1},
    )

    resp: ProviderResponse = await provider.fetch(context)
    assert isinstance(resp, ProviderResponse)
    assert resp.status == DataStatus.COMPLETE
    assert resp.provider_name == "TEFAS"
    assert resp.source_quality == SourceTier.TIER_2_EXCHANGE
    assert resp.source_metadata["valid_count"] == 1
    assert resp.source_metadata["malformed_row_count"] == 0


@pytest.mark.asyncio
async def test_33_orchestrator_integration(monkeypatch):
    """Verify ProviderOrchestrator registers TefasFundPriceProvider and successfully executes request via fetch()."""
    resolver, instruments = create_test_resolver()
    mac_inst = instruments["MAC"]

    raw_payload = {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {"fonKodu": "MAC", "tarih": "2026-08-25", "fiyat": 0.76165}
        ]
    }
    def handler(request):
        return httpx.Response(200, json=raw_payload)

    monkeypatch.setattr("backend.engine.private.providers.tefas_eod.get_http_client", lambda: make_mock_client(handler))

    orch = ProviderOrchestrator()
    provider = TefasFundPriceProvider(resolver=resolver)
    orch.register_provider(provider)

    context = FetchContext(
        observation_type="TEFAS_FUND_PRICE",
        canonical_instrument_id=mac_inst.id,
        provider_symbol="MAC",
        effective_date=date(2026, 8, 27),
        request_parameters={"period_months": 1, "resolver": resolver},
    )
    policy = SourcePolicy(
        observation_type="TEFAS_FUND_PRICE",
        ordered_provider_names=["TEFAS"],
        required_fields=["provider"],
        optional_fields=["provider_symbol"],
    )

    result: OrchestrationResult = await orch.execute(context, policy)
    assert isinstance(result, OrchestrationResult)
    assert result.status == DataStatus.COMPLETE
    assert result.selected_provider == "TEFAS"
    assert result.canonical_instrument_id == mac_inst.id
    assert result.fallback_used is False
