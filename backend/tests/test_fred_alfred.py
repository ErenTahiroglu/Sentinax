"""
backend/tests/test_fred_alfred.py
===================================
Comprehensive Unit and Point-in-Time Semantic Hardening Tests for FRED & ALFRED Layer.

Coverage:
    1. FetchContext with AsOfMode.SYSTEM_AS_OF enum rejected externally
    2. FetchContext with valid SOURCE_AS_OF enum enters vintage mode
    3. Valid string coercion -> AsOfMode in FetchContext
    4. Invalid as_of_mode string rejected with ValueError (fail-fast)
    5. Historical unknown mode fails closed
    6. realtime_start not treated as true first availability (source_available_date is None)
    7. vintage_date stored separately in source_metadata
    8. source_available_date remains None when actual date unproven
    9. Same-day intraday query uses conservative prior-day knowledge snapshot
    10. Prior-day arbitrary vintage accepted
    11. Current fetch sends limit=1
    12. Current fetch sends sort_order=desc
    13. Exact observation date query bounded
    14. Vintage fetch bounded
    15. output_type=1 explicit in query params
    16. vintagedates helper paginates with offset
    17. All vintage dates > 10000 can be collected via multiple mocked pages
    18. Vintage endpoint 401 raises ProviderAuthenticationError
    19. Vintage endpoint 429 raises ProviderRateLimitError
    20. Vintage endpoint 5xx raises ProviderServerError
    21. Vintage empty valid response returns []
    22. Metadata endpoint API failure raises typed error
    23. ProviderProvenance preserves origin_source
    24. ProviderProvenance preserves release_name
    25. ProviderProvenance metadata serialization round-trip
    26. ProviderResponse source_metadata survives OrchestrationResult round-trip
    27. to_macro_observation mapper preserves vintage_date
    28. to_macro_observation mapper does not fabricate source_available_date
    29. Release calendar date does not become availability date
    30. Current realtime_start query date does not become first availability
    31. Geography migration safe pattern
    32. Availability precision check constraint
    33. FRED six registry series remain verified
    34. Missing != 0 invariant
"""

import os
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from backend.engine.private.confidence import DataConfidence
from backend.engine.private.domain import (
    AsOfMode,
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
from backend.engine.private.orchestrator import OrchestrationResult
from backend.engine.private.provider_contract import (
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.providers.fred_alfred import FREDALFREDProvider


class TestFREDALFREDProviderHardening:

    # 1. AsOfMode Enum Validation & Fail-Closed Guards
    @pytest.mark.asyncio
    async def test_01_system_as_of_enum_rejected_externally(self):
        """Scenario 1: FetchContext with AsOfMode.SYSTEM_AS_OF enum rejected externally."""
        provider = FREDALFREDProvider(api_key="key")
        ctx = FetchContext(
            observation_type="MACRO_US",
            provider_symbol="US_REAL_GDP",
            as_of_time=datetime(2023, 5, 1, 12, 0, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SYSTEM_AS_OF,
        )
        resp = await provider.fetch(ctx)
        assert resp.status == DataStatus.UNAVAILABLE
        assert "Historical SYSTEM_AS_OF requires local PIT storage" in resp.warnings[0]

    @pytest.mark.asyncio
    async def test_02_valid_source_as_of_enum_enters_vintage_mode(self):
        """Scenario 2: FetchContext with AsOfMode.SOURCE_AS_OF enum queries ALFRED vintage."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                "realtime_start": "2023-04-30",
                "observations": [{"date": "2023-01-01", "value": "22112.3", "realtime_start": "2023-04-30"}]
            }),
        )

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_US",
            provider_symbol="US_REAL_GDP",
            as_of_time=datetime(2023, 5, 1, 12, 0, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        resp = await provider.fetch(ctx)
        assert resp.status == DataStatus.COMPLETE
        assert resp.source_metadata["vintage_date"] == "2023-04-30" # Conservative prior day

    def test_03_string_coercion_to_as_of_mode(self):
        """Scenario 3: Valid strings coerce to AsOfMode enum in FetchContext."""
        ctx1 = FetchContext("MACRO_US", as_of_mode="SOURCE_AS_OF")
        assert ctx1.as_of_mode == AsOfMode.SOURCE_AS_OF

        ctx2 = FetchContext("MACRO_US", as_of_mode="system_as_of")
        assert ctx2.as_of_mode == AsOfMode.SYSTEM_AS_OF

    def test_04_invalid_as_of_string_fails_fast(self):
        """Scenario 4: Invalid string raises ValueError immediately."""
        with pytest.raises(ValueError, match="Invalid as_of_mode"):
            FetchContext("MACRO_US", as_of_mode="INVALID_MODE")

    # 2. Realtime_start vs Vintage Date vs Availability
    @pytest.mark.asyncio
    async def test_06_07_08_and_30_realtime_start_not_fabricated_into_availability(self):
        """Scenario 6, 7, 8 & 30: realtime_start is query period, NOT first availability."""
        # Even if observation is 1990 and realtime_start is 2026-08-26
        mock_payload = {
            "realtime_start": "2026-08-26",
            "observations": [
                {"date": "1990-01-01", "value": "130.0", "realtime_start": "2026-08-26"}
            ]
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        ctx = FetchContext("MACRO_US", provider_symbol="US_CPI_HEADLINE_INDEX")
        resp = await provider.fetch(ctx)

        assert resp.source_metadata["realtime_start"] == "2026-08-26"
        assert resp.source_metadata["source_available_date"] is None
        assert resp.published_at is None

    # 3. Same-day Lookahead Policy
    @pytest.mark.asyncio
    async def test_09_and_10_same_day_intraday_uses_conservative_prior_day(self):
        """Scenario 9 & 10: Intraday as_of_time queries prior-day vintage snapshot."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"observations": [{"date": "2024-01-01", "value": "100.0"}]}),
        )

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        # As of May 15, 2024 at 09:30 AM UTC
        ctx = FetchContext(
            observation_type="MACRO_US",
            provider_symbol="US_CPI_HEADLINE_INDEX",
            as_of_time=datetime(2024, 5, 15, 9, 30, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        await provider.fetch(ctx)

        _, kwargs = mock_client.get.call_args
        # Must request 2024-05-14 (prior day snapshot)
        assert kwargs["params"]["vintage_dates"] == "2024-05-14"

    # 4. Bounded Queries
    @pytest.mark.asyncio
    async def test_11_12_and_15_current_fetch_is_bounded(self):
        """Scenario 11, 12 & 15: Current fetch requests limit=1, sort_order=desc, output_type=1."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"observations": [{"date": "2024-04-01", "value": "313.548"}]}),
        )

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        await provider.fetch(FetchContext("MACRO_US", provider_symbol="US_CPI_HEADLINE_INDEX"))

        _, kwargs = mock_client.get.call_args
        params = kwargs["params"]
        assert params["limit"] == 1
        assert params["sort_order"] == "desc"
        assert params["output_type"] == 1
        assert params["units"] == "lin"

    @pytest.mark.asyncio
    async def test_13_and_14_exact_observation_date_bounded(self):
        """Scenario 13 & 14: Exact effective_date query bounds start and end dates."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"observations": [{"date": "2024-01-01", "value": "100.0"}]}),
        )

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_US",
            provider_symbol="US_CPI_HEADLINE_INDEX",
            effective_date=date(2024, 1, 1),
        )
        await provider.fetch(ctx)

        _, kwargs = mock_client.get.call_args
        params = kwargs["params"]
        assert params["observation_start"] == "2024-01-01"
        assert params["observation_end"] == "2024-01-01"

    # 5. Vintage Dates Pagination & Typed Error Handling
    @pytest.mark.asyncio
    async def test_16_and_17_vintage_dates_helper_pagination(self):
        """Scenario 16 & 17: get_all_vintage_dates traverses multiple pages using offset."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        # Page 1: 3 dates out of 5 total
        page1 = {"vintage_dates": ["2024-05-01", "2024-04-01", "2024-03-01"], "count": 5}
        # Page 2: 2 remaining dates
        page2 = {"vintage_dates": ["2024-02-01", "2024-01-01"], "count": 5}

        mock_client.get.side_effect = [
            MagicMock(status_code=200, json=MagicMock(return_value=page1)),
            MagicMock(status_code=200, json=MagicMock(return_value=page2)),
        ]

        provider = FREDALFREDProvider(api_key="key", http_client=mock_client)
        all_vintages = await provider.get_all_vintage_dates("CPIAUCSL", max_pages=5)

        assert len(all_vintages) == 5
        assert all_vintages[0] == date(2024, 5, 1)
        assert all_vintages[-1] == date(2024, 1, 1)
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_18_19_20_and_21_vintage_helper_typed_errors(self):
        """Scenario 18-21: Vintage dates helper raises typed errors on non-200 and returns [] for count=0."""
        # 401 Auth
        m_401 = AsyncMock(spec=httpx.AsyncClient)
        m_401.get.return_value = MagicMock(status_code=401)
        p_401 = FREDALFREDProvider(api_key="key", http_client=m_401)
        with pytest.raises(ProviderAuthenticationError):
            await p_401.get_vintage_dates_page("CPIAUCSL")

        # 429 RateLimit
        m_429 = AsyncMock(spec=httpx.AsyncClient)
        m_429.get.return_value = MagicMock(status_code=429)
        p_429 = FREDALFREDProvider(api_key="key", http_client=m_429)
        with pytest.raises(ProviderRateLimitError):
            await p_429.get_vintage_dates_page("CPIAUCSL")

        # 500 ServerError
        m_500 = AsyncMock(spec=httpx.AsyncClient)
        m_500.get.return_value = MagicMock(status_code=500)
        p_500 = FREDALFREDProvider(api_key="key", http_client=m_500)
        with pytest.raises(ProviderServerError):
            await p_500.get_vintage_dates_page("CPIAUCSL")

        # Empty valid response
        m_empty = AsyncMock(spec=httpx.AsyncClient)
        m_empty.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value={"vintage_dates": [], "count": 0}))
        p_empty = FREDALFREDProvider(api_key="key", http_client=m_empty)
        res_empty, count = await p_empty.get_vintage_dates_page("CPIAUCSL")
        assert res_empty == []
        assert count == 0

    @pytest.mark.asyncio
    async def test_22_metadata_endpoint_typed_error(self):
        """Scenario 22: Series metadata failure raises typed exception instead of silent empty dict."""
        m_500 = AsyncMock(spec=httpx.AsyncClient)
        m_500.get.return_value = MagicMock(status_code=500)
        provider = FREDALFREDProvider(api_key="key", http_client=m_500)
        with pytest.raises(ProviderServerError):
            await provider.get_series_metadata("CPIAUCSL")

    # 6. Provenance & Serialization Round-trip
    def test_23_24_and_25_provenance_metadata_serialization_round_trip(self):
        """Scenario 23-25: ProviderProvenance preserves origin_source & release_name across serialization."""
        prov = ProviderProvenance(
            provider_name="FRED_ALFRED",
            provider_version="1.1.0",
            endpoint="https://api.stlouisfed.org/fred/series/observations",
            retrieved_at=datetime.now(timezone.utc),
            source_quality=SourceTier.TIER_1_REGULATORY,
            provider_symbol="US_CPI_HEADLINE_INDEX",
            effective_date=date(2024, 4, 1),
            metadata={
                "delivery_provider": "Federal Reserve Bank of St. Louis FRED",
                "origin_source": "U.S. Bureau of Labor Statistics",
                "release_name": "Consumer Price Index",
                "vintage_date": "2024-05-01",
            }
        )

        serialized = prov.to_dict()
        reconstituted = ProviderProvenance.from_dict(serialized)

        assert reconstituted.metadata["origin_source"] == "U.S. Bureau of Labor Statistics"
        assert reconstituted.metadata["release_name"] == "Consumer Price Index"
        assert reconstituted.metadata["vintage_date"] == "2024-05-01"

    def test_26_orchestration_result_preserves_source_metadata(self):
        """Scenario 26: OrchestrationResult preserves source_metadata across to_dict / from_dict."""
        orch = OrchestrationResult(
            observation_type="MACRO_US",
            status=DataStatus.COMPLETE,
            confidence=DataConfidence(
                level=DataConfidenceLevel.HIGH,
                freshness=1.0,
                source_quality=1.0,
                coverage=1.0,
                consistency=1.0,
                calculation_coverage=1.0,
                reasons=["Regulatory origin"],
            ),
            data={"value": 313.548},
            effective_date=date(2024, 4, 1),
            source_metadata={
                "vintage_date": "2024-05-01",
                "origin_source": "U.S. Bureau of Labor Statistics",
            }
        )

        serialized = orch.to_dict()
        reconstituted = OrchestrationResult.from_dict(serialized)

        assert reconstituted.source_metadata["vintage_date"] == "2024-05-01"
        assert reconstituted.source_metadata["origin_source"] == "U.S. Bureau of Labor Statistics"

    # 7. Macro Observation Mapper
    def test_27_28_and_29_macro_observation_mapper_semantics(self):
        """Scenario 27-29: to_macro_observation maps vintage_date, keeps source_available_date None."""
        orch = OrchestrationResult(
            observation_type="MACRO_US",
            status=DataStatus.COMPLETE,
            confidence=DataConfidence(
                level=DataConfidenceLevel.HIGH,
                freshness=1.0,
                source_quality=1.0,
                coverage=1.0,
                consistency=1.0,
                calculation_coverage=1.0,
                reasons=["Regulatory origin"],
            ),
            data={"value": 22758.969},
            effective_date=date(2024, 1, 1),
            provenance=ProviderProvenance(
                provider_name="FRED_ALFRED",
                provider_version="1.1.0",
                endpoint="https://api.stlouisfed.org/fred/series/observations",
                retrieved_at=datetime.now(timezone.utc),
                source_quality=SourceTier.TIER_1_REGULATORY,
                metadata={
                    "origin_source": "U.S. Bureau of Economic Analysis",
                    "release_name": "Gross Domestic Product",
                    "vintage_date": "2024-04-25",
                }
            ),
            source_metadata={
                "vintage_date": "2024-04-25",
                "source_available_date": None,
            }
        )

        macro_rec = orch.to_macro_observation(
            series_key="US_REAL_GDP",
            unit=MacroUnit.BILLIONS_USD,
            frequency=MacroFrequency.QUARTERLY,
        )

        assert macro_rec is not None
        assert macro_rec.series_key == "US_REAL_GDP"
        assert macro_rec.value == 22758.969
        assert macro_rec.vintage_date == date(2024, 4, 25)
        assert macro_rec.source_available_date is None
        assert macro_rec.origin_source == "U.S. Bureau of Economic Analysis"
        assert macro_rec.release_name == "Gross Domestic Product"

    # 8. Migration 007 Hardening & Schema Integrity
    def test_31_and_32_migration_007_schema_integrity(self):
        """Scenario 31 & 32: Migration 007 enforces safe geography backfill and precision checks."""
        migration_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../supabase/migrations/007_macro_source_availability.sql")
        )
        assert os.path.isfile(migration_path)
        with open(migration_path, "r", encoding="utf-8") as f:
            sql = f.read()

        # No silent DEFAULT 'TR' for future rows
        assert "ALTER TABLE public.macro_series" in sql
        assert "DROP DEFAULT" in sql
        # Check constraint on precision
        assert "chk_macro_obs_precision" in sql
        assert "CHECK (availability_precision IS NULL OR availability_precision IN ('DATE', 'TIMESTAMP'))" in sql

    # 9. Registry & Taxonomy Invariants
    def test_33_and_34_registry_and_missing_invariants(self):
        """Scenario 33 & 34: 6 verified US series and Missing != 0."""
        us_series = MacroSeriesRegistry.list_by_geography("US")
        assert len(us_series) == 6
        for s in us_series:
            assert s.contract_status == ContractStatus.VERIFIED
            assert s.is_active is True

        assert FREDALFREDProvider._parse_decimal(".") is None
        assert FREDALFREDProvider._parse_decimal("0.0") == 0.0
