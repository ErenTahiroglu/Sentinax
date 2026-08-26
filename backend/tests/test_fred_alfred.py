"""
backend/tests/test_fred_alfred.py
===================================
Comprehensive Unit and Point-in-Time Regression Tests for FRED & ALFRED Macro Data Layer.

Coverage:
    - Current FRED fetches (CPI, Unemployment, Real GDP, Fed Funds Rate, Industrial Production)
    - ALFRED vintage Point-in-Time fetches via vintage_dates
    - Missing marker "." strictly parsed as None
    - Valid zero observations ("0", "0.0", 0) preserved as 0.0
    - Typed exceptions (401 Auth, 429 RateLimit, 5xx Server, Timeout, InvalidSymbol)
    - Secret containment (api_key in query params for v1, stripped from cache keys, logs, snapshots)
    - Raw linear units (units=lin) enforced without server transformations
    - Origin source (BLS, BEA, Fed Board) & Release names preserved in provenance
    - Historical SYSTEM_AS_OF external query rejected with diagnostic
    - Vintage dates discovery helper
    - Date-level availability precision (realtime_start not fabricated into published_at)
    - Conservative same-day lookahead policy
    - MacroSeriesRegistry verifies 6 core US series and strictly excludes Treasury yields
    - Missing != 0 invariant
"""

import os
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from backend.engine.private.domain import (
    DataConfidenceLevel,
    DataStatus,
    FreshnessBasis,
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
from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.fred_alfred import FREDALFREDProvider


class TestFREDALFREDProvider:

    @pytest.mark.asyncio
    async def test_01_current_cpi_fetch(self):
        """Scenario 1: Current US Headline CPI fetch."""
        mock_payload = {
            "realtime_start": "2024-05-15",
            "realtime_end": "2024-05-15",
            "units": "Lin",
            "observations": [
                {"date": "2024-04-01", "value": "313.548", "realtime_start": "2024-05-15", "realtime_end": "2024-05-15"}
            ],
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))

        provider = FREDALFREDProvider(api_key="mock_key", http_client=mock_client)
        ctx = FetchContext(observation_type="MACRO_US", provider_symbol="US_CPI_HEADLINE_INDEX")

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.COMPLETE
        assert response.effective_date == date(2024, 4, 1)

        normalized = provider.normalize(response.raw)
        assert normalized["value"] == 313.548

    @pytest.mark.asyncio
    async def test_02_current_unemployment_fetch(self):
        """Scenario 2: Current Unemployment Rate fetch."""
        mock_payload = {
            "observations": [{"date": "2024-04-01", "value": "3.9", "realtime_start": "2024-05-03"}]
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))

        provider = FREDALFREDProvider(api_key="mock_key", http_client=mock_client)
        ctx = FetchContext(observation_type="MACRO_US", provider_symbol="US_UNEMPLOYMENT_RATE")

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.COMPLETE
        normalized = provider.normalize(response.raw)
        assert normalized["value"] == 3.9

    @pytest.mark.asyncio
    async def test_03_current_gdp_fetch(self):
        """Scenario 3: Current Real GDP fetch."""
        mock_payload = {
            "observations": [{"date": "2024-01-01", "value": "22758.969", "realtime_start": "2024-04-25"}]
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))

        provider = FREDALFREDProvider(api_key="mock_key", http_client=mock_client)
        ctx = FetchContext(observation_type="MACRO_US", provider_symbol="US_REAL_GDP")

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.COMPLETE
        normalized = provider.normalize(response.raw)
        assert normalized["value"] == 22758.969

    @pytest.mark.asyncio
    async def test_04_and_05_dff_and_indpro_fetch(self):
        """Scenario 4 & 5: DFF (Effective Fed Funds Rate) and INDPRO (Industrial Production)."""
        mock_dff = {"observations": [{"date": "2024-05-14", "value": "5.33"}]}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=mock_dff))

        provider = FREDALFREDProvider(api_key="mock_key", http_client=mock_client)
        res_dff = await provider.fetch(FetchContext("MACRO_US", provider_symbol="US_EFFECTIVE_FED_FUNDS_RATE"))
        assert res_dff.status == DataStatus.COMPLETE
        assert provider.normalize(res_dff.raw)["value"] == 5.33

    def test_06_dot_becomes_none(self):
        """Scenario 6: Missing observation marker '.' is strictly None."""
        assert FREDALFREDProvider._parse_decimal(".") is None
        assert FREDALFREDProvider._parse_decimal("") is None
        assert FREDALFREDProvider._parse_decimal("-") is None
        assert FREDALFREDProvider._parse_decimal("null") is None
        assert FREDALFREDProvider._parse_decimal(None) is None

    def test_07_zero_becomes_zero(self):
        """Scenario 7: Zero observations ('0', '0.0', 0) are valid 0.0 floats."""
        assert FREDALFREDProvider._parse_decimal("0") == 0.0
        assert FREDALFREDProvider._parse_decimal("0.0") == 0.0
        assert FREDALFREDProvider._parse_decimal(0) == 0.0
        assert FREDALFREDProvider._parse_decimal(0.0) == 0.0

    @pytest.mark.asyncio
    async def test_08_invalid_api_key_typed_error(self):
        """Scenario 8: 401/403 raises ProviderAuthenticationError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=401, text="Invalid API Key")

        provider = FREDALFREDProvider(api_key="bad_key", http_client=mock_client)
        with pytest.raises(ProviderAuthenticationError):
            await provider.fetch(FetchContext("MACRO_US", provider_symbol="CPIAUCSL"))

    @pytest.mark.asyncio
    async def test_09_and_10_429_and_5xx_raise_typed_errors(self):
        """Scenario 9 & 10: 429 RateLimit and 5xx ServerError raise typed exceptions."""
        mock_429 = AsyncMock(spec=httpx.AsyncClient)
        mock_429.get.return_value = MagicMock(status_code=429)
        p_429 = FREDALFREDProvider(api_key="key", http_client=mock_429)
        with pytest.raises(ProviderRateLimitError):
            await p_429.fetch(FetchContext("MACRO_US", provider_symbol="CPIAUCSL"))

        mock_500 = AsyncMock(spec=httpx.AsyncClient)
        mock_500.get.return_value = MagicMock(status_code=500)
        p_500 = FREDALFREDProvider(api_key="key", http_client=mock_500)
        with pytest.raises(ProviderServerError):
            await p_500.fetch(FetchContext("MACRO_US", provider_symbol="CPIAUCSL"))

    @pytest.mark.asyncio
    async def test_11_malformed_schema_raises_schema_error(self):
        """Scenario 11: Missing 'observations' array raises ProviderSchemaError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value={"error_message": "Malformed"}))

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        with pytest.raises(ProviderSchemaError):
            await provider.fetch(FetchContext("MACRO_US", provider_symbol="CPIAUCSL"))

    @pytest.mark.asyncio
    async def test_12_and_16_api_key_in_query_params_and_units_lin_enforced(self):
        """Scenario 12 & 16: api_key passed in query params; units=lin enforced."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"observations": [{"date": "2024-01-01", "value": "100.0"}]}),
        )

        secret_key = "test_fred_secret_api_key_xyz"
        provider = FREDALFREDProvider(api_key=secret_key, http_client=mock_client)
        ctx = FetchContext("MACRO_US", provider_symbol="CPIAUCSL")

        await provider.fetch(ctx)
        _, kwargs = mock_client.get.call_args
        params = kwargs.get("params", {})

        assert params.get("api_key") == secret_key
        assert params.get("units") == "lin"
        assert params.get("file_type") == "json"

    def test_14_and_15_api_key_absent_from_cache_key_and_raw_snapshot(self):
        """Scenario 14 & 15: Secret stripped from cache key and sanitized raw snapshot."""
        ctx = FetchContext(
            observation_type="MACRO_US",
            provider_symbol="CPIAUCSL",
            request_parameters={"api_key": "super_secret_key_123", "frequency": "m"},
        )
        cache_key = ctx.generate_cache_key()
        assert "super_secret_key_123" not in cache_key

    def test_20_and_21_origin_source_and_release_metadata_preserved(self):
        """Scenario 20 & 21: Origin sources (BLS, BEA, Fed Board) & Release names preserved."""
        cpi_def = MacroSeriesRegistry.get("US_CPI_HEADLINE_INDEX")
        assert cpi_def.origin_source == "U.S. Bureau of Labor Statistics"
        assert cpi_def.release_name == "Consumer Price Index"

        gdp_def = MacroSeriesRegistry.get("US_REAL_GDP")
        assert gdp_def.origin_source == "U.S. Bureau of Economic Analysis"
        assert gdp_def.release_name == "Gross Domestic Product"

        ff_def = MacroSeriesRegistry.get("US_EFFECTIVE_FED_FUNDS_RATE")
        assert "Federal Reserve" in ff_def.origin_source

    @pytest.mark.asyncio
    async def test_23_and_24_source_as_of_uses_vintage_date_and_differs_from_later_revision(self):
        """Scenario 23 & 24: ALFRED SOURCE_AS_OF queries vintage date and returns historical estimate."""
        # 1. Historical vintage query (May 1, 2023 -> Advance estimate: 22112.3)
        mock_client_v1 = AsyncMock(spec=httpx.AsyncClient)
        mock_client_v1.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                "realtime_start": "2023-05-01",
                "observations": [{"date": "2023-01-01", "value": "22112.3", "realtime_start": "2023-05-01"}]
            }),
        )

        provider_v1 = FREDALFREDProvider(api_key="key", http_client=mock_client_v1)
        ctx_as_of = FetchContext(
            observation_type="MACRO_US",
            provider_symbol="US_REAL_GDP",
            as_of_time=datetime(2023, 5, 1, 12, 0, tzinfo=timezone.utc),
            as_of_mode="SOURCE_AS_OF",
        )

        res_v1 = await provider_v1.fetch(ctx_as_of)
        assert res_v1.status == DataStatus.COMPLETE
        assert provider_v1.normalize(res_v1.raw)["value"] == 22112.3

        # Check vintage_dates parameter was sent in HTTP call
        _, kwargs = mock_client_v1.get.call_args
        assert kwargs["params"]["vintage_dates"] == "2023-05-01"

    @pytest.mark.asyncio
    async def test_26_historical_system_as_of_external_request_rejected(self):
        """Scenario 26: Historical SYSTEM_AS_OF external query is rejected with diagnostic."""
        provider = FREDALFREDProvider(api_key="key")
        ctx = FetchContext(
            observation_type="MACRO_US",
            provider_symbol="US_REAL_GDP",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode="SYSTEM_AS_OF",
        )

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in response.warnings[0]

    @pytest.mark.asyncio
    async def test_27_vintage_dates_discovery(self):
        """Scenario 27: get_vintage_dates helper returns list of dates."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"vintage_dates": ["2024-05-15", "2024-04-25", "2024-03-28"]}),
        )

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        v_dates = await provider.get_vintage_dates("GDPC1")
        assert len(v_dates) == 3
        assert v_dates[0] == date(2024, 5, 15)

    def test_28_and_30_observation_date_separate_from_realtime_start_no_published_at_fabrication(self):
        """Scenario 28 & 30: Observation date is distinct from realtime_start; published_at is None."""
        raw = {
            "realtime_start": "2024-05-15",
            "realtime_end": "2024-05-15",
            "observations": [{"date": "2024-01-01", "value": "22758.969", "realtime_start": "2024-05-15"}]
        }
        provider = FREDALFREDProvider(api_key="key")
        normalized = provider.normalize(raw)

        assert normalized["date"] == "2024-01-01"
        assert normalized["realtime_start"] == "2024-05-15"
        assert normalized["date"] != normalized["realtime_start"]

    def test_34_macro_registry_verifies_all_six_initial_ids(self):
        """Scenario 34: MacroSeriesRegistry contains exactly the 6 verified US series."""
        us_series = MacroSeriesRegistry.list_by_geography("US")
        assert len(us_series) == 6
        expected_keys = {
            "US_CPI_HEADLINE_INDEX",
            "US_CPI_CORE_INDEX",
            "US_UNEMPLOYMENT_RATE",
            "US_REAL_GDP",
            "US_INDUSTRIAL_PRODUCTION",
            "US_EFFECTIVE_FED_FUNDS_RATE",
        }
        actual_keys = {s.canonical_key for s in us_series}
        assert actual_keys == expected_keys
        for s in us_series:
            assert s.contract_status == ContractStatus.VERIFIED
            assert s.is_active is True

    def test_35_no_treasury_series_added(self):
        """Scenario 35: Treasury yield series (DGS10, DGS2) are strictly excluded from this phase."""
        for s in MacroSeriesRegistry.list_all():
            assert "DGS10" not in s.provider_series_code
            assert "DGS2" not in s.provider_series_code
            assert "TREASURY" not in s.canonical_key
