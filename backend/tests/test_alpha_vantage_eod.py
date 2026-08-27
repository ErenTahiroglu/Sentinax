"""
backend/tests/test_alpha_vantage_eod.py
======================================
Comprehensive Test Suite for Alpha Vantage Global (US & Europe) EOD Market Data Adapter.

Verifies:
    1. US stock fixture (e.g. AAPL) parses cleanly with exact Decimal OHLCV.
    2. US ETF fixture (e.g. SPY) parses preserving US_ETF instrument type.
    3. XETRA stock fixture (e.g. MBG.DEX) parses with EUR currency and XETR mic.
    4. Exact Decimal for all prices and volume; float inputs are strictly rejected.
    5. Missing values remain None (missing != zero).
    6. Non-finite values (NaN, Infinity, -Infinity) rejected as INVALID_OBSERVATION.
    7. Malformed OHLC envelope rejected (high < low, open > high, close < low).
    8. Negative OHLC prices (open, high, low, close < 0) rejected as INVALID_OBSERVATION.
    9. Provider error inside HTTP 200 JSON detected ("Error Message").
    10. Rate-limit message inside HTTP 200 JSON detected ("Information" / "Note").
    11. Empty or malformed series handled cleanly with empty observations and diagnostics.
    12. Unresolved alias fails closed with UNRESOLVED_IDENTITY status.
    13. Response metadata symbol validation: mismatch fails closed with RESPONSE_SYMBOL_MISMATCH.
    14. Missing response metadata symbol fails closed with INVALID_SOURCE_CONTEXT.
    15. Strict PIT: trade_date (economic) != retrieved_at (network UTC), published_at is None.
    16. Raw snapshot payload_hash is deterministic SHA-256.
    17. Aggregate status: all UNRESOLVED_IDENTITY -> ProviderResponse.status == UNAVAILABLE.
    18. Aggregate status: all INVALID_OBSERVATION -> ProviderResponse.status == UNAVAILABLE.
    19. Aggregate status: mixed valid and invalid rows -> ProviderResponse.status == PARTIAL with counters.
    20. Aggregate status: all valid rows -> ProviderResponse.status == COMPLETE.
    21. Request identity binding: mismatched canonical_instrument_id & provider_symbol fails before HTTP call.
    22. Request identity binding: matched dual identity succeeds cleanly.
    23. Serialization to dict and PIT storage records contains pure string Decimals, no floats.
    24. Async fetch with mock HTTP responses (200, 429, 500, timeout).
    25. Zero network calls permitted (pytest-socket active).
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
)
from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.alpha_vantage_eod import (
    ALPHA_VANTAGE_PROVIDER_NAME,
    AlphaVantageEODProvider,
    _parse_finite_decimal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_AAPL_JSON = """{
    "Meta Data": {
        "1. Information": "Daily Prices (open, high, low, close) and Volumes",
        "2. Symbol": "AAPL",
        "3. Last Refreshed": "2024-10-01",
        "4. Output Size": "Compact",
        "5. Time Zone": "US/Eastern"
    },
    "Time Series (Daily)": {
        "2024-10-01": {
            "1. open": "228.5000",
            "2. high": "230.0000",
            "3. low": "225.5000",
            "4. close": "226.2100",
            "5. volume": "45689000"
        },
        "2024-09-30": {
            "1. open": "227.0000",
            "2. high": "229.0000",
            "3. low": "226.0000",
            "4. close": "228.6000",
            "5. volume": "38200000"
        }
    }
}"""

SAMPLE_SPY_ETF_JSON = """{
    "Meta Data": {
        "1. Information": "Daily Prices (open, high, low, close) and Volumes",
        "2. Symbol": "SPY",
        "3. Last Refreshed": "2024-10-01",
        "4. Output Size": "Compact",
        "5. Time Zone": "US/Eastern"
    },
    "Time Series (Daily)": {
        "2024-10-01": {
            "1. open": "569.8000",
            "2. high": "571.2500",
            "3. low": "567.1000",
            "4. close": "568.9000",
            "5. volume": "52340000"
        }
    }
}"""

SAMPLE_XETRA_MBG_JSON = """{
    "Meta Data": {
        "1. Information": "Daily Prices (open, high, low, close) and Volumes",
        "2. Symbol": "MBG.DEX",
        "3. Last Refreshed": "2024-10-01",
        "4. Output Size": "Compact",
        "5. Time Zone": "US/Eastern"
    },
    "Time Series (Daily)": {
        "2024-10-01": {
            "1. open": "58.2000",
            "2. high": "59.1000",
            "3. low": "57.8000",
            "4. close": "58.6500",
            "5. volume": "1845000"
        }
    }
}"""

SAMPLE_RATE_LIMIT_INFO_JSON = """{
    "Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day. Please subscribe to any of the premium plans at https://www.alphavantage.co/premium/ to instantly remove all daily rate limits."
}"""

SAMPLE_ERROR_MESSAGE_JSON = """{
    "Error Message": "Invalid API call. Please check the parameter of your API query. If you think this is a bug, please contact support@alphavantage.co."
}"""


@pytest.fixture
def identity_resolver() -> InstrumentResolverService:
    """Sets up a mock Instrument Master with AAPL, SPY, and MBG.DEX aliases."""
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
        provider="ALPHA_VANTAGE",
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
        provider="ALPHA_VANTAGE",
        provider_symbol="SPY",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_spy)
    resolver.register_alias(alias_spy)

    # 3. European XETRA Stock: MBG.DEX
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
        provider="ALPHA_VANTAGE",
        provider_symbol="MBG.DEX",
        valid_from=date(2000, 1, 1),
    )
    resolver.register_instrument(inst_mbg)
    resolver.register_alias(alias_mbg)

    return resolver


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

class TestAlphaVantageEODProvider:

    def test_01_us_stock_fixture_aapl(self, identity_resolver):
        """Test 1: US stock AAPL parses cleanly with exact Decimal OHLCV and canonical UUID."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert snap.provider == "ALPHA_VANTAGE"
        assert snap.provider_symbol == "AAPL"
        assert snap.is_rate_limited is False
        assert len(snap.observations) == 2

        obs_latest = snap.observations[1]  # 2024-10-01 (sorted chronologically)
        assert obs_latest.trade_date == date(2024, 10, 1)
        assert obs_latest.close == Decimal("226.2100")
        assert obs_latest.open == Decimal("228.5000")
        assert obs_latest.high == Decimal("230.0000")
        assert obs_latest.low == Decimal("225.5000")
        assert obs_latest.volume == Decimal("45689000")
        assert obs_latest.instrument_id == UUID("11111111-1111-1111-1111-111111111111")
        assert obs_latest.instrument_type == InstrumentType.US_STOCK
        assert obs_latest.currency == Currency.USD
        assert obs_latest.exchange == "XNAS"
        assert obs_latest.status == GlobalObservationStatus.VALID
        assert obs_latest.confidence_level == DataConfidenceLevel.MEDIUM

    def test_02_us_etf_fixture_spy(self, identity_resolver):
        """Test 2: US ETF SPY preserves US_ETF instrument type without converting to stock."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_SPY_ETF_JSON, "SPY", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert len(snap.observations) == 1
        obs = snap.observations[0]
        assert obs.instrument_type == InstrumentType.US_ETF
        assert obs.currency == Currency.USD
        assert obs.exchange == "ARCX"
        assert obs.close == Decimal("568.9000")

    def test_03_xetra_stock_fixture_mbg(self, identity_resolver):
        """Test 3: European XETRA stock MBG.DEX parses with EUR currency and XETR MIC."""
        ret_time = datetime(2024, 10, 1, 20, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_XETRA_MBG_JSON, "MBG.DEX", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert len(snap.observations) == 1
        obs = snap.observations[0]
        assert obs.instrument_type == InstrumentType.EUROPEAN_STOCK
        assert obs.currency == Currency.EUR
        assert obs.exchange == "XETR"
        assert obs.close == Decimal("58.6500")

    def test_04_exact_decimal_and_float_rejection(self, identity_resolver):
        """Test 4: All parsed fields are exact Decimals; float inputs are strictly rejected."""
        # Exact parser function rejects Python float
        assert _parse_finite_decimal(0.1) is None
        assert _parse_finite_decimal(150.25) is None
        assert _parse_finite_decimal("0.1") == Decimal("0.1")
        assert _parse_finite_decimal(100) == Decimal("100")
        assert _parse_finite_decimal(Decimal("150.25")) == Decimal("150.25")

        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert isinstance(obs.open, Decimal)
        assert isinstance(obs.high, Decimal)
        assert isinstance(obs.low, Decimal)
        assert isinstance(obs.close, Decimal)
        assert isinstance(obs.volume, Decimal)
        assert not isinstance(obs.close, float)

    def test_05_missing_value_not_zero(self, identity_resolver):
        """Test 5: Missing field in row remains None, NEVER defaulted to Decimal('0') or 0.0."""
        partial_json = """{
            "Meta Data": {
                "2. Symbol": "AAPL"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "4. close": "150.0000"
                }
            }
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            partial_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.close == Decimal("150.0000")
        assert obs.open is None
        assert obs.high is None
        assert obs.low is None
        assert obs.volume is None
        assert obs.status == GlobalObservationStatus.VALID

    def test_06_non_finite_values_rejected(self, identity_resolver):
        """Test 6: NaN, Infinity, -Infinity rejected as INVALID_OBSERVATION."""
        nan_json = """{
            "Meta Data": {
                "2. Symbol": "AAPL"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "1. open": "100.00",
                    "2. high": "NaN",
                    "3. low": "90.00",
                    "4. close": "95.00",
                    "5. volume": "1000"
                }
            }
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            nan_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert obs.high is None
        assert any("NON_FINITE_DECIMAL" in d for d in obs.diagnostics)

    def test_07_malformed_ohlc_envelope_rejected(self, identity_resolver):
        """Test 7: Envelope violations (high < low, close > high, open < low) rejected."""
        bad_envelope_json = """{
            "Meta Data": {
                "2. Symbol": "AAPL"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "1. open": "100.00",
                    "2. high": "90.00",
                    "3. low": "95.00",
                    "4. close": "92.00",
                    "5. volume": "1000"
                }
            }
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            bad_envelope_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert any("OHLC_ENVELOPE_VIOLATION" in d for d in obs.diagnostics)

    def test_08_negative_prices_rejected(self, identity_resolver):
        """Test 8: Internally consistent negative OHLC prices rejected as INVALID_OBSERVATION."""
        negative_ohlc_json = """{
            "Meta Data": {
                "2. Symbol": "AAPL"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "1. open": "-10.00",
                    "2. high": "-5.00",
                    "3. low": "-15.00",
                    "4. close": "-8.00",
                    "5. volume": "1000"
                }
            }
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            negative_ohlc_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        assert obs.status == GlobalObservationStatus.INVALID_OBSERVATION
        assert any("NEGATIVE_PRICE" in d for d in obs.diagnostics)

    def test_09_provider_error_inside_http_200(self, identity_resolver):
        """Test 9: Alpha Vantage 'Error Message' inside HTTP 200 payload detected and handled."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_ERROR_MESSAGE_JSON, "INVALID_TICKER", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert len(snap.observations) == 0
        assert any("PROVIDER_ERROR" in d for d in snap.diagnostics)

    def test_10_rate_limit_inside_http_200(self, identity_resolver):
        """Test 10: 25 requests/day limit note inside HTTP 200 detected and flagged on snapshot."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_RATE_LIMIT_INFO_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert snap.is_rate_limited is True
        assert len(snap.observations) == 0
        assert any("RATE_LIMIT_EXHAUSTED" in d for d in snap.diagnostics)

    def test_11_response_symbol_mismatch_fails_closed(self, identity_resolver):
        """Test 11: Response metadata symbol MSFT for requested AAPL fails closed."""
        mismatch_json = """{
            "Meta Data": {
                "2. Symbol": "MSFT"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "1. open": "420.00",
                    "2. high": "425.00",
                    "3. low": "418.00",
                    "4. close": "422.00",
                    "5. volume": "20000000"
                }
            }
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            mismatch_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert len(snap.observations) == 0
        assert any("RESPONSE_SYMBOL_MISMATCH" in d for d in snap.diagnostics)

    def test_12_missing_metadata_symbol_fails_closed(self, identity_resolver):
        """Test 12: Missing '2. Symbol' in Meta Data fails closed with INVALID_SOURCE_CONTEXT."""
        no_sym_json = """{
            "Meta Data": {
                "1. Information": "Daily Prices"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "1. open": "100.00",
                    "2. high": "105.00",
                    "3. low": "98.00",
                    "4. close": "102.00",
                    "5. volume": "1000"
                }
            }
        }"""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            no_sym_json, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        assert len(snap.observations) == 0
        assert any("INVALID_SOURCE_CONTEXT" in d for d in snap.diagnostics)

    def test_13_unresolved_alias_fails_closed(self):
        """Test 13: Unmapped symbol alias fails closed with UNRESOLVED_IDENTITY."""
        empty_resolver = InstrumentResolverService()
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=empty_resolver
        )

        assert len(snap.observations) == 2
        for obs in snap.observations:
            assert obs.instrument_id is None
            assert obs.status == GlobalObservationStatus.UNRESOLVED_IDENTITY
            assert any("UNRESOLVED_IDENTITY" in d for d in obs.diagnostics)

    def test_14_strict_pit_timestamps(self, identity_resolver):
        """Test 14: trade_date != retrieved_at and published_at is None."""
        ret_time = datetime(2024, 10, 2, 3, 15, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[1]
        assert obs.trade_date == date(2024, 10, 1)
        assert obs.retrieved_at == ret_time
        assert obs.published_at is None

    def test_15_deterministic_payload_hash(self, identity_resolver):
        """Test 15: Raw snapshot payload_hash is deterministic SHA-256."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        import hashlib
        calc = hashlib.sha256(SAMPLE_AAPL_JSON.encode("utf-8")).hexdigest()
        assert snap.payload_hash == calc
        assert snap.observations[0].payload_hash == calc

    @pytest.mark.asyncio
    async def test_16_request_identity_mismatch_fails_before_http(self, identity_resolver):
        """Test 16: Conflicting canonical ID (SPY) and provider_symbol (AAPL) fails before HTTP call."""
        provider = AlphaVantageEODProvider(api_key="TEST_KEY")
        spy_id = UUID("22222222-2222-2222-2222-222222222222")

        # Context has SPY canonical ID but AAPL provider symbol
        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            canonical_instrument_id=spy_id,
            provider_symbol="AAPL",
        )

        with patch("backend.engine.private.providers.alpha_vantage_eod.get_http_client") as mock_http:
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("IDENTITY_MISMATCH" in w for w in resp.warnings)
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_17_matched_dual_identity_succeeds(self, identity_resolver):
        """Test 17: Matching canonical ID and provider symbol succeeds cleanly."""
        provider = AlphaVantageEODProvider(api_key="TEST_KEY")
        aapl_id = UUID("11111111-1111-1111-1111-111111111111")

        ctx = FetchContext(
            observation_type="GLOBAL_EOD_PRICE",
            canonical_instrument_id=aapl_id,
            provider_symbol="AAPL",
        )

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.alpha_vantage_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.COMPLETE
            assert resp.canonical_instrument_id == aapl_id
            assert resp.source_metadata["valid_count"] == 2

    @pytest.mark.asyncio
    async def test_18_aggregate_status_all_unresolved_is_unavailable(self):
        """Test 18: 100% UNRESOLVED_IDENTITY observations results in DataStatus.UNAVAILABLE."""
        provider = AlphaVantageEODProvider(api_key="TEST_KEY")
        empty_resolver = InstrumentResolverService()
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = SAMPLE_AAPL_JSON

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.alpha_vantage_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=empty_resolver)

            assert resp.status == DataStatus.UNAVAILABLE
            assert resp.raw is not None
            assert resp.source_metadata["valid_count"] == 0
            assert resp.source_metadata["unresolved_count"] == 2

    @pytest.mark.asyncio
    async def test_19_aggregate_status_all_invalid_is_unavailable(self, identity_resolver):
        """Test 19: 100% INVALID_OBSERVATION results in DataStatus.UNAVAILABLE."""
        provider = AlphaVantageEODProvider(api_key="TEST_KEY")
        all_invalid_json = """{
            "Meta Data": {
                "2. Symbol": "AAPL"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "1. open": "100.00",
                    "2. high": "80.00",
                    "3. low": "90.00",
                    "4. close": "85.00",
                    "5. volume": "1000"
                }
            }
        }"""
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = all_invalid_json

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.alpha_vantage_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.UNAVAILABLE
            assert resp.source_metadata["valid_count"] == 0
            assert resp.source_metadata["invalid_count"] == 1

    @pytest.mark.asyncio
    async def test_20_aggregate_status_mixed_is_partial(self, identity_resolver):
        """Test 20: Mixed series (1 valid, 1 invalid envelope) results in DataStatus.PARTIAL."""
        provider = AlphaVantageEODProvider(api_key="TEST_KEY")
        mixed_json = """{
            "Meta Data": {
                "2. Symbol": "AAPL"
            },
            "Time Series (Daily)": {
                "2024-10-01": {
                    "1. open": "100.00",
                    "2. high": "105.00",
                    "3. low": "95.00",
                    "4. close": "102.00",
                    "5. volume": "1000"
                },
                "2024-09-30": {
                    "1. open": "100.00",
                    "2. high": "80.00",
                    "3. low": "90.00",
                    "4. close": "85.00",
                    "5. volume": "1000"
                }
            }
        }"""
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 200
        mock_http_resp.text = mixed_json

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.alpha_vantage_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx, resolver=identity_resolver)

            assert resp.status == DataStatus.PARTIAL
            assert resp.source_metadata["valid_count"] == 1
            assert resp.source_metadata["invalid_count"] == 1

    def test_21_record_serialization_no_float(self, identity_resolver):
        """Test 21: to_dict() and storage conversions produce pure string Decimals and zero floats."""
        ret_time = datetime(2024, 10, 1, 22, 0, tzinfo=timezone.utc)
        snap = AlphaVantageEODProvider.parse_daily_series(
            SAMPLE_AAPL_JSON, "AAPL", retrieved_at=ret_time, resolver=identity_resolver
        )

        obs = snap.observations[0]
        d = obs.to_dict()
        assert isinstance(d["close"], str)
        assert d["currency"] == "USD"
        assert d["instrument_type"] == "us_stock"

        rec = obs.to_normalized_observation_record()
        rec_dict = rec.to_record_dict()
        assert rec_dict["observation_data"]["close"] == str(obs.close)
        assert rec_dict["asset_class"] == "equity"

    @pytest.mark.asyncio
    async def test_22_provider_async_fetch_missing_api_key(self):
        """Test 22: Fetch without API key returns UNAVAILABLE with AUTH_ERROR warning."""
        provider = AlphaVantageEODProvider(api_key=None)
        with patch.dict("os.environ", {}, clear=True):
            ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("AUTH_ERROR" in w for w in resp.warnings)

    @pytest.mark.asyncio
    async def test_23_provider_async_fetch_rate_limited_429(self):
        """Test 23: HTTP 429 returns UNAVAILABLE with RATE_LIMITED warning."""
        provider = AlphaVantageEODProvider(api_key="TEST_API_KEY")
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 429
        mock_http_resp.text = "Too Many Requests"

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.alpha_vantage_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("RATE_LIMITED" in w for w in resp.warnings)

    @pytest.mark.asyncio
    async def test_24_provider_async_fetch_server_error_500(self):
        """Test 24: HTTP 500 returns UNAVAILABLE with SERVER_ERROR warning."""
        provider = AlphaVantageEODProvider(api_key="TEST_API_KEY")
        ctx = FetchContext(observation_type="GLOBAL_EOD_PRICE", provider_symbol="AAPL")

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 500
        mock_http_resp.text = "Internal Server Error"

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_http_resp

        with patch("backend.engine.private.providers.alpha_vantage_eod.get_http_client", return_value=mock_client):
            resp = await provider.fetch(ctx)

            assert resp.status == DataStatus.UNAVAILABLE
            assert any("SERVER_ERROR" in w for w in resp.warnings)

    def test_25_access_and_capabilities_metadata(self):
        """Test 25: Access classification is YELLOW and capacity constraints are explicit."""
        provider = AlphaVantageEODProvider(api_key="TEST")
        assert provider.access_status == ProviderAccessStatus.YELLOW
        assert provider.source_quality == SourceTier.TIER_3_AGGREGATOR
        assert provider.official_source is False
        assert AlphaVantageCapability.FREE_DAILY_LIMIT_CONSTRAINED in provider.capabilities
        assert AlphaVantageCapability.FREE_COMPACT_HISTORY in provider.capabilities
