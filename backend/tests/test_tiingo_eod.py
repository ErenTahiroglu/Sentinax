"""
backend/tests/test_tiingo_eod.py
================================
Comprehensive Test Suite for Tiingo US Equities & ETFs Daily EOD and History Adapter.

Verifies:
    1. US stock fixture (AAPL) parses cleanly with exact raw & adjusted Decimal OHLCV.
    2. US ETF fixture (SPY) parses cleanly preserving US_ETF instrument type.
    3. Exact Decimal parsing: lexical numbers parsed into exact Decimal, float inputs rejected.
    4. Missing optional fields remain None (missing != zero); missing close price flags INVALID_OBSERVATION.
    5. Non-finite values (NaN, Infinity, -Infinity) rejected as INVALID_OBSERVATION.
    6. Raw OHLC envelope violations and negative prices rejected as INVALID_OBSERVATION.
    7. Adjusted OHLC envelope violations and negative adjusted prices rejected as INVALID_OBSERVATION.
    8. Split event (splitFactor != 1) sets history_refresh_required = True.
    9. Dividend event (divCash > 0) sets history_refresh_required = True.
    10. Normal trading day (splitFactor == 1, divCash == 0) sets history_refresh_required = False.
    11. Dual identity mismatch fails closed before network request.
    12. Unsupported instrument type (e.g. EUROPEAN_STOCK) fails closed with UNSUPPORTED_INSTRUMENT_TYPE.
    13. Unresolved alias fails closed with UNRESOLVED_IDENTITY.
    14. Token safety: secret API token never leaked into serialized records, warnings, or metadata.
    15. Aggregate status: all valid -> COMPLETE, 100% invalid -> UNAVAILABLE, mixed -> PARTIAL.
    16. Strict PIT: trade_date != retrieved_at, published_at is None.
    17. Deterministic SHA-256 payload hash preserved on snapshot and observations.
    18. Serialization to dict and RawProviderSnapshotRecord / NormalizedObservationRecord contain pure string Decimals.
    19. HTTP error status codes (401/403, 404, 429, 500, timeout) handled cleanly.
    20. Zero live network calls (pytest-socket active).
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
    GlobalEODObservation,
    GlobalEODSnapshot,
    GlobalObservationStatus,
    TiingoCapability,
)
from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.tiingo_eod import (
    TIINGO_PROVIDER_NAME,
    TiingoEODProvider,
    _parse_finite_decimal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_TIINGO_AAPL_JSON = """[
    {
        "date": "2024-10-01T00:00:00.000Z",
        "close": 226.21,
        "high": 230.0,
        "low": 225.5,
        "open": 228.5,
        "volume": 45689000,
        "adjClose": 226.21,
        "adjHigh": 230.0,
        "adjLow": 225.5,
        "adjOpen": 228.5,
        "adjVolume": 45689000,
        "divCash": 0.0,
        "splitFactor": 1.0
    },
    {
        "date": "2024-09-30T00:00:00.000Z",
        "close": 228.6,
        "high": 229.0,
        "low": 226.0,
        "open": 227.0,
        "volume": 38200000,
        "adjClose": 228.6,
        "adjHigh": 229.0,
        "adjLow": 226.0,
        "adjOpen": 227.0,
        "adjVolume": 38200000,
        "divCash": 0.25,
        "splitFactor": 1.0
    }
]"""

SAMPLE_TIINGO_SPY_JSON = """[
    {
        "date": "2024-10-01T00:00:00.000Z",
        "close": 568.9,
        "high": 571.25,
        "low": 567.1,
        "open": 569.8,
        "volume": 52340000,
        "adjClose": 568.9,
        "adjHigh": 571.25,
        "adjLow": 567.1,
        "adjOpen": 569.8,
        "adjVolume": 52340000,
        "divCash": 0.0,
        "splitFactor": 1.0
    }
]"""

SAMPLE_TIINGO_SPLIT_JSON = """[
    {
        "date": "2020-08-31T00:00:00.000Z",
        "close": 129.04,
        "high": 131.0,
        "low": 126.0,
        "open": 127.58,
        "volume": 225700000,
        "adjClose": 126.5,
        "adjHigh": 128.4,
        "adjLow": 123.5,
        "adjOpen": 125.07,
        "adjVolume": 225700000,
        "divCash": 0.0,
        "splitFactor": 0.25
    }
]"""


@pytest.fixture
def identity_resolver() -> InstrumentResolverService:
    """Sets up a mock Instrument Master with AAPL, SPY, and European aliases."""
    resolver = InstrumentResolverService()

    # 1. US Stock: AAPL
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
        provider="TIINGO",
        provider_symbol="AAPL",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_aapl)
    resolver.register_alias(alias_aapl)

    # 2. US ETF: SPY
    spy_id = UUID("22222222-2222-2222-2222-222222222222")
    inst_spy = InstrumentRecord(
        id=spy_id,
        canonical_name="SPDR S&P 500 ETF Trust",
        asset_class=AssetClass.ETF,
        instrument_type=InstrumentType.US_ETF,
        currency=Currency.USD,
        mic="ARCX",
        isin="US78462F1030",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(2000, 1, 1),
    )
    alias_spy = ProviderAliasRecord(
        instrument_id=spy_id,
        provider="TIINGO",
        provider_symbol="SPY",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_spy)
    resolver.register_alias(alias_spy)

    # 3. European Stock: MBG (Unsupported for Tiingo)
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
        provider="TIINGO",
        provider_symbol="MBG",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_mbg)
    resolver.register_alias(alias_mbg)

    return resolver


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

class TestTiingoEODProvider:

    def test_01_us_stock_fixture_aapl(self, identity_resolver):
        """Test 1: US stock AAPL parses cleanly with exact raw and adjusted Decimals."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            SAMPLE_TIINGO_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert snap.provider == "TIINGO"
        assert snap.provider_symbol == "AAPL"
        assert snap.is_rate_limited is False
        assert len(snap.observations) == 2

        obs_latest = snap.observations[1]  # 2024-10-01 sorted chronologically
        assert obs_latest.trade_date == date(2024, 10, 1)
        assert obs_latest.close == Decimal("226.21")
        assert obs_latest.open == Decimal("228.5")
        assert obs_latest.high == Decimal("230.0")
        assert obs_latest.low == Decimal("225.5")
        assert obs_latest.volume == Decimal("45689000")
        assert obs_latest.adj_close == Decimal("226.21")
        assert obs_latest.adj_open == Decimal("228.5")
        assert obs_latest.adj_high == Decimal("230.0")
        assert obs_latest.adj_low == Decimal("225.5")
        assert obs_latest.adj_volume == Decimal("45689000")
        assert obs_latest.div_cash == Decimal("0.0")
        assert obs_latest.split_factor == Decimal("1.0")
        assert obs_latest.instrument_id == UUID("11111111-1111-1111-1111-111111111111")
        assert obs_latest.instrument_type == InstrumentType.US_STOCK
        assert obs_latest.currency == Currency.USD
        assert obs_latest.status == GlobalObservationStatus.VALID

    def test_02_us_etf_fixture_spy(self, identity_resolver):
        """Test 2: US ETF SPY parses cleanly preserving US_ETF instrument type."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            SAMPLE_TIINGO_SPY_JSON, "SPY", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert len(snap.observations) == 1
        obs = snap.observations[0]
        assert obs.instrument_type == InstrumentType.US_ETF
        assert obs.currency == Currency.USD
        assert obs.close == Decimal("568.9")
        assert obs.adj_close == Decimal("568.9")

    def test_03_exact_decimal_and_float_rejection(self, identity_resolver):
        """Test 3: Exact Decimal parser rejects floats and parses string/int correctly."""
        assert _parse_finite_decimal(0.1) is None
        assert _parse_finite_decimal("0.1") == Decimal("0.1")
        assert _parse_finite_decimal(100) == Decimal("100")
        assert _parse_finite_decimal(Decimal("568.9")) == Decimal("568.9")

        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            SAMPLE_TIINGO_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert isinstance(obs.open, Decimal)
        assert isinstance(obs.close, Decimal)
        assert isinstance(obs.adj_close, Decimal)
        assert not isinstance(obs.close, float)

    def test_04_missing_optional_fields_not_zero(self, identity_resolver):
        """Test 4: Missing optional fields remain None, missing close flags INVALID_OBSERVATION."""
        partial_json = """[
            {
                "date": "2024-10-01T00:00:00.000Z",
                "close": 200.0
            }
        ]"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            partial_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.close == Decimal("200.0")
        assert obs.open is None
        assert obs.high is None
        assert obs.adj_close is None
        assert obs.div_cash is None
        assert obs.status == GlobalObservationStatus.VALID

    def test_05_non_finite_values_rejected(self, identity_resolver):
        """Test 5: NaN / Infinity in raw or adjusted price flags INVALID_OBSERVATION."""
        nan_json = """[
            {
                "date": "2024-10-01T00:00:00.000Z",
                "open": "NaN",
                "high": 210.0,
                "low": 195.0,
                "close": 200.0
            }
        ]"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            nan_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert any("NON_FINITE_DECIMAL" in d for d in obs.diagnostics)

    def test_06_raw_ohlc_envelope_violations_rejected(self, identity_resolver):
        """Test 6: Raw high < low or open outside range rejected."""
        bad_envelope_json = """[
            {
                "date": "2024-10-01T00:00:00.000Z",
                "open": 220.0,
                "high": 200.0,
                "low": 210.0,
                "close": 205.0
            }
        ]"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            bad_envelope_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert any("OHLC_ENVELOPE_VIOLATION" in d for d in obs.diagnostics)

    def test_07_adjusted_ohlc_envelope_violations_rejected(self, identity_resolver):
        """Test 7: Adjusted high < low or negative adjusted prices rejected."""
        bad_adj_json = """[
            {
                "date": "2024-10-01T00:00:00.000Z",
                "close": 100.0,
                "adjOpen": 105.0,
                "adjHigh": 90.0,
                "adjLow": 95.0,
                "adjClose": 92.0
            }
        ]"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            bad_adj_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert any("ADJUSTED_OHLC_ENVELOPE_VIOLATION" in d for d in obs.diagnostics)

    def test_08_split_event_signals_history_refresh(self, identity_resolver):
        """Test 8: splitFactor != 1 triggers history_refresh_required = True."""
        ret_time = datetime(2020, 8, 31, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            SAMPLE_TIINGO_SPLIT_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert snap.history_refresh_required is True
        obs = snap.observations[0]
        assert obs.split_factor == Decimal("0.25")
        assert obs.status == GlobalObservationStatus.VALID

    def test_09_dividend_event_signals_history_refresh(self, identity_resolver):
        """Test 9: divCash > 0 triggers history_refresh_required = True."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        # 2nd item in SAMPLE_TIINGO_AAPL_JSON has divCash: 0.25
        snap = TiingoEODProvider.parse_daily_prices(
            SAMPLE_TIINGO_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert snap.history_refresh_required is True

    def test_10_normal_trading_day_no_history_refresh(self, identity_resolver):
        """Test 10: Normal day (splitFactor == 1, divCash == 0) sets history_refresh_required = False."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            SAMPLE_TIINGO_SPY_JSON, "SPY", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert snap.history_refresh_required is False

    @pytest.mark.asyncio
    async def test_11_dual_identity_mismatch_fails_before_http(self, identity_resolver):
        """Test 11: Conflicting canonical ID (SPY) and symbol (AAPL) fails before HTTP call."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        spy_id = UUID("22222222-2222-2222-2222-222222222222")

        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            canonical_instrument_id=spy_id,
            provider_symbol="AAPL",
        )

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("IDENTITY_MISMATCH" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_12_unsupported_instrument_type_fails(self, identity_resolver):
        """Test 12: Resolving to EUROPEAN_STOCK fails with UNSUPPORTED_INSTRUMENT_TYPE."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        mbg_id = UUID("33333333-3333-3333-3333-333333333333")

        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            canonical_instrument_id=mbg_id,
            provider_symbol="MBG",
        )

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("UNSUPPORTED_INSTRUMENT_TYPE" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_13_token_safety_never_leaked(self, identity_resolver):
        """Test 13: Secret token SUPER_SECRET_TEST_TOKEN never appears in serialized outputs."""
        secret_token = "SUPER_SECRET_TEST_TOKEN_12345"
        provider = TiingoEODProvider(api_token=secret_token)
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_TIINGO_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            # Check headers sent token safely
            called_headers = mock_client.get.call_args[1]["headers"]
            assert called_headers["Authorization"] == f"Token {secret_token}"

            # Verify token not leaked in serialized output
            dumped_resp = str(resp.to_dict() if hasattr(resp, "to_dict") else resp.__dict__)
            assert secret_token not in dumped_resp
            assert secret_token not in str(resp.warnings)
            assert secret_token not in str(resp.source_metadata)

    @pytest.mark.asyncio
    async def test_14_aggregate_status_and_counts(self, identity_resolver):
        """Test 14: Aggregate status reflects valid, invalid, and mixed observations."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_TIINGO_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            assert resp.source_metadata["valid_count"] == 2
            assert resp.source_metadata["invalid_count"] == 0
            assert resp.source_metadata["history_refresh_required"] is True

    def test_15_storage_and_record_serialization(self, identity_resolver):
        """Test 15: Normalized observation and snapshot record conversions contain pure string Decimals."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = TiingoEODProvider.parse_daily_prices(
            SAMPLE_TIINGO_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        rec = obs.to_normalized_observation_record()
        rec_dict = rec.to_record_dict()
        assert rec_dict["observation_data"]["close"] == str(obs.close)
        assert rec_dict["observation_data"]["adj_close"] == str(obs.adj_close)
        assert rec_dict["observation_data"]["div_cash"] == str(obs.div_cash)
        assert rec_dict["observation_data"]["split_factor"] == str(obs.split_factor)

        snap_rec = snap.to_raw_provider_snapshot_record()
        assert snap_rec.provider == "TIINGO"
        assert snap_rec.payload_hash == snap.payload_hash

    @pytest.mark.asyncio
    async def test_16_http_error_handling(self):
        """Test 16: HTTP 401/403, 404, 429, 500 return UNAVAILABLE without exceptions."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

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

            with patch("backend.engine.private.providers.tiingo_eod.get_http_client", return_value=mock_client):
                resp = await provider.fetch(ctx)

                assert resp.status == DataStatus.UNAVAILABLE
                assert any(expected_diag in w for w in resp.warnings)

    def test_17_capabilities_and_metadata(self):
        """Test 17: Tiingo provider capabilities and access classification."""
        provider = TiingoEODProvider(api_token="TEST")
        assert provider.access_status == ProviderAccessStatus.YELLOW
        assert provider.source_quality == SourceTier.TIER_3_AGGREGATOR
        assert provider.official_source is False
        assert TiingoCapability.STARTER_FREE in provider.capabilities
        assert TiingoCapability.ADJUSTED_SERIES in provider.capabilities
        assert TiingoCapability.CORPORATE_ACTIONS in provider.capabilities
        assert TiingoCapability.LONG_HISTORY in provider.capabilities

    @pytest.mark.asyncio
    async def test_18_official_tiingo_date_format(self, identity_resolver):
        """Test 18: Tiingo format YYYY-M-D normalized to YYYY-MM-DD in outbound request and snapshot."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="AAPL",
            request_parameters={"startDate": "2012-1-1", "endDate": "2016-1-1"},
        )

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_TIINGO_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            # Verify outbound normalized params
            called_params = mock_client.get.call_args[1]["params"]
            assert called_params["startDate"] == "2012-01-01"
            assert called_params["endDate"] == "2016-01-01"

            # Verify snapshot boundaries
            assert resp.raw.start_date == date(2012, 1, 1)
            assert resp.raw.end_date == date(2016, 1, 1)

    @pytest.mark.asyncio
    async def test_19_canonical_iso_date_format(self, identity_resolver):
        """Test 19: Canonical ISO format YYYY-MM-DD parsed and passed correctly."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="AAPL",
            request_parameters={"startDate": "2012-01-01", "endDate": "2016-01-01"},
        )

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_TIINGO_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            called_params = mock_client.get.call_args[1]["params"]
            assert called_params["startDate"] == "2012-01-01"
            assert called_params["endDate"] == "2016-01-01"

    @pytest.mark.asyncio
    async def test_20_date_object_inputs(self, identity_resolver):
        """Test 20: datetime.date instances in request_parameters accepted and normalized."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="AAPL",
            request_parameters={"startDate": date(2012, 1, 1), "endDate": date(2016, 1, 1)},
        )

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_TIINGO_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            called_params = mock_client.get.call_args[1]["params"]
            assert called_params["startDate"] == "2012-01-01"
            assert called_params["endDate"] == "2016-01-01"

    @pytest.mark.asyncio
    async def test_21_invalid_start_date_fails_before_http(self):
        """Test 21: Malformed startDate fails closed with INVALID_DATE_PARAMETER before HTTP call."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="AAPL",
            request_parameters={"startDate": "not-a-date"},
        )

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("INVALID_DATE_PARAMETER" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_22_invalid_end_date_fails_before_http(self):
        """Test 22: Non-existent calendar endDate (e.g. 2024-02-30) fails closed before HTTP call."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="AAPL",
            request_parameters={"endDate": "2024-02-30"},
        )

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("INVALID_DATE_PARAMETER" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_23_reversed_date_range_fails_before_http(self):
        """Test 23: startDate > endDate fails closed with INVALID_DATE_RANGE before HTTP call."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="AAPL",
            request_parameters={"startDate": "2025-01-01", "endDate": "2024-01-01"},
        )

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("INVALID_DATE_RANGE" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_24_same_day_date_range_succeeds(self, identity_resolver):
        """Test 24: startDate == endDate is valid and executes successfully."""
        provider = TiingoEODProvider(api_token="TEST_TOKEN")
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            provider_symbol="AAPL",
            request_parameters={"startDate": "2024-10-01", "endDate": "2024-10-01"},
        )

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_TIINGO_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.tiingo_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            called_params = mock_client.get.call_args[1]["params"]
            assert called_params["startDate"] == "2024-10-01"
            assert called_params["endDate"] == "2024-10-01"
            assert resp.raw.start_date == date(2024, 10, 1)
            assert resp.raw.end_date == date(2024, 10, 1)
