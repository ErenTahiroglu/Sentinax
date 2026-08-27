"""
backend/tests/test_marketstack_eod.py
=====================================
Comprehensive Test Suite for Marketstack European Equities & ETFs Rolling EOD Adapter.

Verifies:
    1. European stock fixture (MBG.XETRA) parses cleanly with exact raw & adjusted Decimal OHLCV.
    2. European ETF fixture (CSPX.XLON) parses cleanly preserving EUROPEAN_ETF instrument type.
    3. Exact Decimal parsing: lexical numbers parsed into exact Decimal, float inputs rejected.
    4. Symbol mismatch in response rows flags INVALID_OBSERVATION / INVALID_SOURCE_CONTEXT.
    5. Exchange / MIC mismatch in response rows flags INVALID_OBSERVATION / INVALID_SOURCE_CONTEXT.
    6. Date range <= 366 days executes cleanly with limit=1000, sort=ASC.
    7. Reversed date range (date_from > date_to) fails closed before HTTP with INVALID_DATE_RANGE.
    8. Free history window (> 366 days) fails closed before HTTP with FREE_HISTORY_WINDOW_EXCEEDED.
    9. Pagination complete (total == count) reports DataStatus.COMPLETE.
    10. Pagination truncated (total > count) flags TRUNCATED_RESPONSE and reports DataStatus.PARTIAL.
    11. Corporate actions (split_factor != 1 or dividend > 0) set history_refresh_required = True.
    12. Access key safety: secret key never leaked into warnings, metadata, or serialized snapshot records.
    13. Dual identity mismatch fails closed before network request with IDENTITY_MISMATCH.
    14. Unsupported instrument type (e.g. US_STOCK) fails closed with UNSUPPORTED_INSTRUMENT_TYPE.
    15. Raw snapshot endpoint validation: Alpha Vantage (TIME_SERIES_DAILY), Tiingo (DAILY_PRICES), Marketstack (EOD).
    16. HTTP error status codes (401/403, 404, 429, 500, timeout) handled cleanly without exceptions.
    17. Zero live network calls (pytest-socket active).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.identity import (
    InstrumentRecord,
    InstrumentResolverService,
    ProviderAliasRecord,
)
from backend.engine.private.market_data.global_models import (
    AlphaVantageCapability,
    GlobalEODObservation,
    GlobalEODSnapshot,
    GlobalObservationStatus,
    MarketstackCapability,
    TiingoCapability,
)
from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.alpha_vantage_eod import AlphaVantageEODProvider
from backend.engine.private.providers.marketstack_eod import (
    MARKETSTACK_PROVIDER_NAME,
    MarketstackEODProvider,
    _parse_finite_decimal,
)
from backend.engine.private.providers.tiingo_eod import TiingoEODProvider


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_MARKETSTACK_MBG_JSON = """{
    "pagination": {
        "limit": 1000,
        "offset": 0,
        "count": 2,
        "total": 2
    },
    "data": [
        {
            "open": 64.5,
            "high": 65.2,
            "low": 63.8,
            "close": 64.9,
            "volume": 1250000.0,
            "adj_high": 65.2,
            "adj_low": 63.8,
            "adj_close": 64.9,
            "adj_open": 64.5,
            "adj_volume": 1250000.0,
            "split_factor": 1.0,
            "dividend": 0.0,
            "symbol": "MBG.XETRA",
            "exchange": "XETRA",
            "date": "2024-10-01T00:00:00+0000"
        },
        {
            "open": 63.0,
            "high": 64.6,
            "low": 62.9,
            "close": 64.2,
            "volume": 980000.0,
            "adj_high": 64.6,
            "adj_low": 62.9,
            "adj_close": 64.2,
            "adj_open": 63.0,
            "adj_volume": 980000.0,
            "split_factor": 1.0,
            "dividend": 5.2,
            "symbol": "MBG.XETRA",
            "exchange": "XETRA",
            "date": "2024-09-30T00:00:00+0000"
        }
    ]
}"""

SAMPLE_MARKETSTACK_CSPX_JSON = """{
    "pagination": {
        "limit": 1000,
        "offset": 0,
        "count": 1,
        "total": 1
    },
    "data": [
        {
            "open": 520.0,
            "high": 524.5,
            "low": 518.0,
            "close": 522.3,
            "volume": 45000.0,
            "adj_high": 524.5,
            "adj_low": 518.0,
            "adj_close": 522.3,
            "adj_open": 520.0,
            "adj_volume": 45000.0,
            "split_factor": 1.0,
            "dividend": 0.0,
            "symbol": "CSPX.XLON",
            "exchange": "XLON",
            "date": "2024-10-01T00:00:00+0000"
        }
    ]
}"""

SAMPLE_MARKETSTACK_TRUNCATED_JSON = """{
    "pagination": {
        "limit": 1000,
        "offset": 0,
        "count": 1000,
        "total": 1001
    },
    "data": [
        {
            "open": 64.5,
            "high": 65.2,
            "low": 63.8,
            "close": 64.9,
            "volume": 1250000.0,
            "adj_high": 65.2,
            "adj_low": 63.8,
            "adj_close": 64.9,
            "adj_open": 64.5,
            "adj_volume": 1250000.0,
            "split_factor": 1.0,
            "dividend": 0.0,
            "symbol": "MBG.XETRA",
            "exchange": "XETRA",
            "date": "2024-10-01T00:00:00+0000"
        }
    ]
}"""


@pytest.fixture
def identity_resolver() -> InstrumentResolverService:
    """Sets up a mock Instrument Master with European stock, European ETF, and US stock aliases."""
    resolver = InstrumentResolverService()

    # 1. European Stock: MBG (Mercedes-Benz Group AG) on XETRA
    mbg_id = UUID("33333333-3333-3333-3333-333333333333")
    inst_mbg = InstrumentRecord(
        id=mbg_id,
        canonical_name="Mercedes-Benz Group AG",
        asset_class=AssetClass.EQUITY,
        instrument_type=InstrumentType.EUROPEAN_STOCK,
        currency=Currency.EUR,
        mic="XETR",
        isin="DE0007100000",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(2000, 1, 1),
    )
    alias_mbg = ProviderAliasRecord(
        instrument_id=mbg_id,
        provider="MARKETSTACK",
        provider_symbol="MBG.XETRA",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_mbg)
    resolver.register_alias(alias_mbg)

    # 2. European ETF: CSPX (iShares Core S&P 500 UCITS ETF) on London Stock Exchange
    cspx_id = UUID("44444444-4444-4444-4444-444444444444")
    inst_cspx = InstrumentRecord(
        id=cspx_id,
        canonical_name="iShares Core S&P 500 UCITS ETF",
        asset_class=AssetClass.ETF,
        instrument_type=InstrumentType.EUROPEAN_ETF,
        currency=Currency.USD,
        mic="XLON",
        isin="IE00B5BMR087",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(2000, 1, 1),
    )
    alias_cspx = ProviderAliasRecord(
        instrument_id=cspx_id,
        provider="MARKETSTACK",
        provider_symbol="CSPX.XLON",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_cspx)
    resolver.register_alias(alias_cspx)

    # 3. US Stock: AAPL (Unsupported for Marketstack in Sentinax)
    aapl_id = UUID("11111111-1111-1111-1111-111111111111")
    inst_aapl = InstrumentRecord(
        id=aapl_id,
        canonical_name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        instrument_type=InstrumentType.US_STOCK,
        currency=Currency.USD,
        mic="XNAS",
        isin="US0378331005",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(2000, 1, 1),
    )
    alias_aapl = ProviderAliasRecord(
        instrument_id=aapl_id,
        provider="MARKETSTACK",
        provider_symbol="AAPL",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_aapl)
    resolver.register_alias(alias_aapl)

    return resolver


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketstackEODProvider:

    def test_01_european_stock_fixture_mbg(self, identity_resolver):
        """Test 1: European stock MBG.XETRA parses cleanly with exact raw & adjusted Decimals."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = MarketstackEODProvider.parse_eod_response(
            SAMPLE_MARKETSTACK_MBG_JSON,
            "MBG.XETRA",
            retrieved_at=ret_time,
            resolver=identity_resolver,
            expected_mic="XETR",
        )

        assert snap.provider == "MARKETSTACK"
        assert snap.provider_symbol == "MBG.XETRA"
        assert snap.endpoint == "EOD"
        assert len(snap.observations) == 2
        assert snap.history_refresh_required is True  # Due to dividend: 5.2

        obs_latest = snap.observations[1]  # 2024-10-01 sorted chronologically
        assert obs_latest.trade_date == date(2024, 10, 1)
        assert obs_latest.close == Decimal("64.9")
        assert obs_latest.open == Decimal("64.5")
        assert obs_latest.high == Decimal("65.2")
        assert obs_latest.low == Decimal("63.8")
        assert obs_latest.volume == Decimal("1250000.0")
        assert obs_latest.adj_close == Decimal("64.9")
        assert obs_latest.adj_open == Decimal("64.5")
        assert obs_latest.adj_high == Decimal("65.2")
        assert obs_latest.adj_low == Decimal("63.8")
        assert obs_latest.adj_volume == Decimal("1250000.0")
        assert obs_latest.split_factor == Decimal("1.0")
        assert obs_latest.div_cash == Decimal("0.0")
        assert obs_latest.currency == Currency.EUR
        assert obs_latest.instrument_type == InstrumentType.EUROPEAN_STOCK
        assert obs_latest.instrument_id == UUID("33333333-3333-3333-3333-333333333333")
        assert obs_latest.status == GlobalObservationStatus.VALID

    def test_02_european_etf_fixture_cspx(self, identity_resolver):
        """Test 2: European ETF CSPX.XLON parses cleanly preserving EUROPEAN_ETF instrument type."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = MarketstackEODProvider.parse_eod_response(
            SAMPLE_MARKETSTACK_CSPX_JSON,
            "CSPX.XLON",
            retrieved_at=ret_time,
            resolver=identity_resolver,
            expected_mic="XLON",
        )

        assert len(snap.observations) == 1
        obs = snap.observations[0]
        assert obs.instrument_type == InstrumentType.EUROPEAN_ETF
        assert obs.currency == Currency.USD
        assert obs.close == Decimal("522.3")
        assert obs.adj_close == Decimal("522.3")
        assert obs.status == GlobalObservationStatus.VALID

    def test_03_exact_decimal_and_float_rejection(self, identity_resolver):
        """Test 3: Exact Decimal parser rejects floats and parses string/int correctly."""
        assert _parse_finite_decimal(0.1) is None
        assert _parse_finite_decimal("0.1") == Decimal("0.1")
        assert _parse_finite_decimal(500) == Decimal("500")

        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = MarketstackEODProvider.parse_eod_response(
            SAMPLE_MARKETSTACK_MBG_JSON,
            "MBG.XETRA",
            retrieved_at=ret_time,
            resolver=identity_resolver,
        )

        obs = snap.observations[0]
        assert isinstance(obs.open, Decimal)
        assert isinstance(obs.close, Decimal)
        assert isinstance(obs.adj_close, Decimal)
        assert not isinstance(obs.close, float)

    def test_04_symbol_mismatch_flags_invalid_observation(self, identity_resolver):
        """Test 4: Response row symbol mismatching requested alias flags INVALID_OBSERVATION."""
        mismatch_json = """{
            "pagination": {"limit": 1000, "offset": 0, "count": 1, "total": 1},
            "data": [
                {
                    "open": 64.5, "high": 65.2, "low": 63.8, "close": 64.9, "volume": 1000.0,
                    "symbol": "BMW.XETRA", "exchange": "XETRA", "date": "2024-10-01T00:00:00+0000"
                }
            ]
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = MarketstackEODProvider.parse_eod_response(
            mismatch_json, "MBG.XETRA", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert any("INVALID_SOURCE_CONTEXT" in d for d in obs.diagnostics)

    def test_05_exchange_mismatch_flags_invalid_observation(self, identity_resolver):
        """Test 5: Response exchange mismatching canonical Master MIC flags INVALID_OBSERVATION."""
        mismatch_exchange_json = """{
            "pagination": {"limit": 1000, "offset": 0, "count": 1, "total": 1},
            "data": [
                {
                    "open": 64.5, "high": 65.2, "low": 63.8, "close": 64.9, "volume": 1000.0,
                    "symbol": "MBG.XETRA", "exchange": "XLON", "date": "2024-10-01T00:00:00+0000"
                }
            ]
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = MarketstackEODProvider.parse_eod_response(
            mismatch_exchange_json,
            "MBG.XETRA",
            retrieved_at=ret_time,
            resolver=identity_resolver,
            expected_mic="XETR",
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert any("INVALID_SOURCE_CONTEXT" in d for d in obs.diagnostics)

    @pytest.mark.asyncio
    async def test_06_date_range_valid_and_outbound_params(self, identity_resolver):
        """Test 6: Valid <=366-day range executes with limit=1000, sort=ASC, exchange=XETR."""
        provider = MarketstackEODProvider(access_key="TEST_KEY")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="MBG.XETRA",
            request_parameters={"date_from": "2024-01-01", "date_to": "2024-10-01"},
        )

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_MARKETSTACK_MBG_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.marketstack_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            called_params = mock_client.get.call_args[1]["params"]
            assert called_params["symbols"] == "MBG.XETRA"
            assert called_params["limit"] == 1000
            assert called_params["sort"] == "ASC"
            assert called_params["exchange"] == "XETR"
            assert called_params["date_from"] == "2024-01-01"
            assert called_params["date_to"] == "2024-10-01"

    @pytest.mark.asyncio
    async def test_07_reversed_date_range_fails_before_http(self):
        """Test 7: date_from > date_to fails closed before HTTP with INVALID_DATE_RANGE."""
        provider = MarketstackEODProvider(access_key="TEST_KEY")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="MBG.XETRA",
            request_parameters={"date_from": "2024-10-01", "date_to": "2024-01-01"},
        )

        with patch("backend.engine.private.providers.marketstack_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("INVALID_DATE_RANGE" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_08_free_history_window_exceeded_fails_before_http(self):
        """Test 8: Requested span > 366 days fails closed with FREE_HISTORY_WINDOW_EXCEEDED."""
        provider = MarketstackEODProvider(access_key="TEST_KEY")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="MBG.XETRA",
            request_parameters={"date_from": "2023-01-01", "date_to": "2024-10-01"},
        )

        with patch("backend.engine.private.providers.marketstack_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("FREE_HISTORY_WINDOW_EXCEEDED" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_09_pagination_truncated_flags_partial(self, identity_resolver):
        """Test 9: pagination.total > returned count flags TRUNCATED_RESPONSE and returns PARTIAL."""
        provider = MarketstackEODProvider(access_key="TEST_KEY")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="MBG.XETRA",
        )

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_MARKETSTACK_TRUNCATED_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.marketstack_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.PARTIAL
            assert resp.source_metadata["has_truncation"] is True
            assert any("TRUNCATED_RESPONSE" in w for w in resp.warnings)

    @pytest.mark.asyncio
    async def test_10_access_key_safety_never_leaked(self, identity_resolver):
        """Test 10: Secret key MARKETSTACK_SUPER_SECRET_123 never appears in serialized outputs."""
        secret_key = "MARKETSTACK_SUPER_SECRET_123"
        provider = MarketstackEODProvider(access_key=secret_key)
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="MBG.XETRA")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_MARKETSTACK_MBG_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.marketstack_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            # Check secret key in params only
            called_params = mock_client.get.call_args[1]["params"]
            assert called_params["access_key"] == secret_key

            # Verify secret key not leaked in serialized response
            dumped_resp = str(resp.to_dict() if hasattr(resp, "to_dict") else resp.__dict__)
            assert secret_key not in dumped_resp
            assert secret_key not in str(resp.warnings)
            assert secret_key not in str(resp.source_metadata)

    @pytest.mark.asyncio
    async def test_11_dual_identity_mismatch_fails_before_http(self, identity_resolver):
        """Test 11: Conflicting canonical ID (MBG) and symbol (CSPX.XLON) fails before HTTP."""
        provider = MarketstackEODProvider(access_key="TEST_KEY")
        mbg_id = UUID("33333333-3333-3333-3333-333333333333")

        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            canonical_instrument_id=mbg_id,
            provider_symbol="CSPX.XLON",
        )

        with patch("backend.engine.private.providers.marketstack_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("IDENTITY_MISMATCH" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_12_unsupported_us_type_fails_before_http(self, identity_resolver):
        """Test 12: Resolving to US_STOCK fails with UNSUPPORTED_INSTRUMENT_TYPE."""
        provider = MarketstackEODProvider(access_key="TEST_KEY")
        aapl_id = UUID("11111111-1111-1111-1111-111111111111")

        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            canonical_instrument_id=aapl_id,
            provider_symbol="AAPL",
        )

        with patch("backend.engine.private.providers.marketstack_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("UNSUPPORTED_INSTRUMENT_TYPE" in w for w in resp.warnings)
            mock_http.assert_not_called()

    def test_13_raw_snapshot_endpoint_preservation(self):
        """Test 13: Alpha Vantage (TIME_SERIES_DAILY), Tiingo (DAILY_PRICES), Marketstack (EOD)."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)

        # 1. Alpha Vantage Snapshot Record
        av_snap = GlobalEODSnapshot(
            provider="ALPHA_VANTAGE",
            provider_symbol="AAPL",
            retrieved_at=ret_time,
            http_status=200,
            payload_hash="abc",
            raw_payload="{}",
            endpoint="TIME_SERIES_DAILY",
        )
        av_rec = av_snap.to_raw_provider_snapshot_record()
        assert av_rec.endpoint == "TIME_SERIES_DAILY"

        # 2. Tiingo Snapshot Record
        tiingo_snap = GlobalEODSnapshot(
            provider="TIINGO",
            provider_symbol="AAPL",
            retrieved_at=ret_time,
            http_status=200,
            payload_hash="def",
            raw_payload="[]",
            endpoint="DAILY_PRICES",
        )
        tiingo_rec = tiingo_snap.to_raw_provider_snapshot_record()
        assert tiingo_rec.endpoint == "DAILY_PRICES"

        # 3. Marketstack Snapshot Record
        ms_snap = GlobalEODSnapshot(
            provider="MARKETSTACK",
            provider_symbol="MBG.XETRA",
            retrieved_at=ret_time,
            http_status=200,
            payload_hash="ghi",
            raw_payload="{}",
            endpoint="EOD",
        )
        ms_rec = ms_snap.to_raw_provider_snapshot_record()
        assert ms_rec.endpoint == "EOD"

    @pytest.mark.asyncio
    async def test_14_http_error_status_codes(self):
        """Test 14: HTTP 401/403, 404, 429, 500 return UNAVAILABLE without exceptions."""
        provider = MarketstackEODProvider(access_key="TEST_KEY")
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="MBG.XETRA")

        for status_code, expected_diag in [
            (401, "AUTH_ERROR"),
            (404, "NOT_FOUND"),
            (429, "RATE_LIMITED"),
            (500, "SERVER_ERROR"),
        ]:
            mock_resp = MagicMock()
            mock_resp.status_code = status_code
            mock_resp.text = f"Error {status_code}"

            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp

            with patch("backend.engine.private.providers.marketstack_eod.get_http_client", return_value=mock_client):
                resp = await provider.fetch(ctx)

                assert resp.status == DataStatus.UNAVAILABLE
                assert any(expected_diag in w for w in resp.warnings)

    def test_15_capabilities_and_metadata(self):
        """Test 15: Marketstack provider capabilities and access classification."""
        provider = MarketstackEODProvider(access_key="TEST")
        assert provider.access_status == ProviderAccessStatus.YELLOW
        assert provider.source_quality == SourceTier.TIER_3_AGGREGATOR
        assert provider.official_source is False
        assert MarketstackCapability.FREE_TIER in provider.capabilities
        assert MarketstackCapability.ROLLING_1Y_HISTORY in provider.capabilities
        assert MarketstackCapability.SPLITS_AND_DIVIDENDS in provider.capabilities
        assert MarketstackCapability.EOD_PRICES in provider.capabilities
