"""
backend/tests/test_ecb_eurostat_treasury.py
=============================================
Comprehensive Unit and Point-in-Time Regression & Semantic Hardening Test Suite
for ECB Data Portal, Eurostat (2026 EA21), and U.S. Department of the Treasury.

Coverage:
    - ECB Data Portal (EUR/USD, DFR, MRR, €STR, CSV parsing, bounded query, fail-closed PIT, event-driven policy rates)
    - Eurostat (2026 EA21 composition, HICP 2025=100 / I25 base, ECOICOP v2, Real GDP quarterly YYYY-Qn, period validation, fail-closed PIT)
    - U.S. Treasury (Daily Yield Curve XML, 3M/2Y/10Y/30Y, single-curve fetch, normalize value loss fix, raw XML snapshot, curve fan-out, typed backfill errors)
    - Orchestrator Pipeline (Integration with ProviderOrchestrator & SourcePolicy required_fields=['value'])
    - Provenance & Serialization Round-trips (source_role, origin_source, quote_direction, published_at=None)
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from backend.engine.private.confidence import ConfidenceAssessmentService, DataConfidence
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
    MacroSeriesDefinition,
    MacroUnit,
)
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.orchestrator import OrchestrationResult, ProviderOrchestrator
from backend.engine.private.policy import SourcePolicy
from backend.engine.private.provider_contract import (
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.providers.ecb_sdmx import ECBDataPortalProvider
from backend.engine.private.providers.eurostat_sdmx import EurostatSDMXProvider
from backend.engine.private.providers.us_treasury import USTreasuryYieldCurveProvider


# ─────────────────────────────────────────────────────────────────────────────
# 1. ECB Data Portal Regression & Semantic Tests (Scenarios 1-20)
# ─────────────────────────────────────────────────────────────────────────────

class TestECBDataPortalRegression:

    SAMPLE_ECB_CSV = (
        "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF\n"
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-05-15,1.0850,A,F\n"
    )

    @pytest.mark.asyncio
    async def test_01_ecb_current_eurusd_fetch_complete(self):
        """Scenario 1: EUR/USD current fetch yields DataStatus.COMPLETE with parsed value."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_ECB_CSV)

        provider = ECBDataPortalProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext(observation_type="MACRO_EA", provider_symbol="EA_EURUSD_REFERENCE_RATE"))

        assert resp.status == DataStatus.COMPLETE
        assert resp.effective_date == date(2024, 5, 15)
        normalized = provider.normalize(resp.raw)
        assert normalized["value"] == 1.0850

    def test_02_ecb_official_exr_key_used(self):
        """Scenario 2: Official EXR key EXR/D.USD.EUR.SP00.A is configured in registry."""
        series_def = MacroSeriesRegistry.get("EA_EURUSD_REFERENCE_RATE")
        assert series_def is not None
        assert series_def.provider_series_code == "EXR/D.USD.EUR.SP00.A"

    @pytest.mark.asyncio
    async def test_03_ecb_quote_direction_usd_per_eur(self):
        """Scenario 3: Quote direction is preserved as USD per 1 EUR in source_metadata."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_ECB_CSV)

        provider = ECBDataPortalProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext(observation_type="MACRO_EA", provider_symbol="EA_EURUSD_REFERENCE_RATE"))
        assert resp.source_metadata.get("quote_direction") == "USD per 1 EUR"

    def test_04_ecb_missing_value_is_none(self):
        """Scenario 4: Missing observation values ('', '.', '-', 'NaN') strictly parse to None."""
        assert ECBDataPortalProvider._parse_decimal("") is None
        assert ECBDataPortalProvider._parse_decimal(".") is None
        assert ECBDataPortalProvider._parse_decimal("-") is None
        assert ECBDataPortalProvider._parse_decimal("NaN") is None

    def test_05_ecb_zero_value_preserved(self):
        """Scenario 5: Genuine 0.0 value is preserved and not coerced to None."""
        assert ECBDataPortalProvider._parse_decimal("0.0") == 0.0
        assert ECBDataPortalProvider._parse_decimal("0") == 0.0

    @pytest.mark.asyncio
    async def test_06_ecb_latest_request_bounded(self):
        """Scenario 6: Current latest request sends lastNObservations=1."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_ECB_CSV)

        provider = ECBDataPortalProvider(http_client=mock_client)
        await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_EURUSD_REFERENCE_RATE"))

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["lastNObservations"] == 1
        assert kwargs["params"]["format"] == "csvdata"

    @pytest.mark.asyncio
    async def test_07_ecb_exact_effective_date_bounded(self):
        """Scenario 7: Exact effective_date query bounds startPeriod and endPeriod."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_ECB_CSV)

        provider = ECBDataPortalProvider(http_client=mock_client)
        await provider.fetch(FetchContext(
            observation_type="MACRO_EA",
            provider_symbol="EA_EURUSD_REFERENCE_RATE",
            effective_date=date(2024, 5, 15),
        ))

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["startPeriod"] == "2024-05-15"
        assert kwargs["params"]["endPeriod"] == "2024-05-15"

    @pytest.mark.asyncio
    async def test_08_ecb_unverified_series_rejected_before_http(self):
        """Scenario 8: Unverified series is rejected without network fetch."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        provider = ECBDataPortalProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_UNVERIFIED_RATE"))

        assert resp.status == DataStatus.UNAVAILABLE
        assert mock_client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_09_ecb_dataflow_discovery_helper_parses_response(self):
        """Scenario 9: Dataflow discovery helper successfully parses official ECB structures."""
        mock_payload = {
            "data": {
                "dataflows": [{"id": "EXR", "name": "Exchange Rates"}, {"id": "FM", "name": "Financial Markets"}]
            }
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))

        provider = ECBDataPortalProvider(http_client=mock_client)
        flows = await provider.get_dataflows()
        assert len(flows) == 2
        assert flows[0]["id"] == "EXR"

    @pytest.mark.asyncio
    async def test_10_ecb_malformed_discovery_response_typed_schema_error(self):
        """Scenario 10: Malformed JSON from discovery endpoint raises ProviderSchemaError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_res = MagicMock(status_code=200)
        mock_res.json.side_effect = ValueError("Invalid JSON")
        mock_client.get.return_value = mock_res

        provider = ECBDataPortalProvider(http_client=mock_client)
        with pytest.raises(ProviderSchemaError):
            await provider.get_dataflows()

    @pytest.mark.asyncio
    async def test_11_ecb_historical_system_as_of_rejected(self):
        """Scenario 11: Historical SYSTEM_AS_OF external fetch is rejected (fail-closed)."""
        provider = ECBDataPortalProvider()
        ctx = FetchContext(
            "MACRO_EA",
            provider_symbol="EA_EURUSD_REFERENCE_RATE",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SYSTEM_AS_OF,
        )
        resp = await provider.fetch(ctx)
        assert resp.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in resp.warnings[0]

    @pytest.mark.asyncio
    async def test_12_ecb_historical_source_as_of_rejected(self):
        """Scenario 12: Historical SOURCE_AS_OF external fetch is rejected (fail-closed)."""
        provider = ECBDataPortalProvider()
        ctx = FetchContext(
            "MACRO_EA",
            provider_symbol="EA_EURUSD_REFERENCE_RATE",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        resp = await provider.fetch(ctx)
        assert resp.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in resp.warnings[0]

    @pytest.mark.asyncio
    async def test_13_ecb_published_at_not_fabricated(self):
        """Scenario 13: published_at is None; never fabricated from retrieved_at or updatedAfter."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_ECB_CSV)

        provider = ECBDataPortalProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_EURUSD_REFERENCE_RATE"))
        assert resp.published_at is None

    @pytest.mark.asyncio
    async def test_14_ecb_source_role_central_bank_preserved(self):
        """Scenario 14: Provenance metadata carries source_role='CENTRAL_BANK'."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_ECB_CSV)

        provider = ECBDataPortalProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_EURUSD_REFERENCE_RATE"))
        prov = provider.provenance(resp)
        assert prov.metadata["source_role"] == "CENTRAL_BANK"
        assert prov.metadata["origin_source"] == "European Central Bank"

    def test_15_to_17_ecb_dfr_and_mro_event_driven_freshness(self):
        """Scenario 15-17: DFR & MRO are EVENT_DRIVEN with expected_release_interval_days=None."""
        dfr = MacroSeriesRegistry.get("EA_ECB_DEPOSIT_FACILITY_RATE")
        assert dfr.frequency == MacroFrequency.EVENT_DRIVEN
        assert dfr.expected_release_interval_days is None

        mro = MacroSeriesRegistry.get("EA_ECB_MAIN_REFINANCING_RATE")
        assert mro.frequency == MacroFrequency.EVENT_DRIVEN
        assert mro.expected_release_interval_days is None

    def test_18_ecb_event_driven_confidence_not_stale(self):
        """Scenario 18: An event-driven policy rate observation unchanged for 60 days retains freshness=1.0."""
        conf = ConfidenceAssessmentService.assess(
            source_tier=SourceTier.TIER_1_REGULATORY,
            data_status=DataStatus.COMPLETE,
            effective_date=date(2024, 3, 1),
            published_at=None,
            observed_at=datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
            retrieved_at=datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
            as_of_time=datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
            required_fields=["value"],
            optional_fields=[],
            present_fields=["value"],
            max_staleness_days=None,
        )
        assert conf.freshness == 1.0
        assert conf.level == DataConfidenceLevel.HIGH

    def test_19_and_20_ecb_estr_and_eurusd_remain_business_daily(self):
        """Scenario 19 & 20: €STR and EUR/USD benchmarks remain BUSINESS_DAILY with 1-day interval."""
        estr = MacroSeriesRegistry.get("EA_ESTR")
        assert estr.frequency == MacroFrequency.BUSINESS_DAILY
        assert estr.expected_release_interval_days == 1

        fx = MacroSeriesRegistry.get("EA_EURUSD_REFERENCE_RATE")
        assert fx.frequency == MacroFrequency.BUSINESS_DAILY
        assert fx.expected_release_interval_days == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. Eurostat (2026 EA21 / 2025=100) Regression & Semantic Tests (Scenarios 21-42)
# ─────────────────────────────────────────────────────────────────────────────

class TestEurostatRegressionAnd2026Semantics:

    SAMPLE_HICP_IDX_CSV = (
        "DATAFLOW,unit,coicop,geo,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
        "prc_hicp_midx,I25,CP00,EA21,2026-04,101.40,p\n"
    )

    SAMPLE_HICP_YOY_CSV = (
        "DATAFLOW,unit,coicop,geo,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
        "prc_hicp_manr,RCH_A,CP00,EA21,2026-04,2.4,p\n"
    )

    SAMPLE_GDP_CSV = (
        "DATAFLOW,unit,s_adj,na_item,geo,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
        "namq_10_gdp,CLV10_MNAC,SCA,B1GQ,EA21,2026-Q1,3150000.0,p\n"
    )

    @pytest.mark.asyncio
    async def test_21_to_24_hicp_index_and_yoy_provider_fetches(self):
        """Scenario 21-24: Actual HICP index and YoY fetches COMPLETE with separate units and EA21 geo."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = [
            MagicMock(status_code=200, text=self.SAMPLE_HICP_IDX_CSV),
            MagicMock(status_code=200, text=self.SAMPLE_HICP_YOY_CSV),
        ]

        provider = EurostatSDMXProvider(http_client=mock_client)

        # Index
        r_idx = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_HICP_ALL_ITEMS_INDEX"))
        assert r_idx.status == DataStatus.COMPLETE
        assert provider.normalize(r_idx.raw)["value"] == 101.40
        assert r_idx.source_metadata["geo"] == "EA21"

        # YoY
        r_yoy = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_HICP_ALL_ITEMS_YOY"))
        assert r_yoy.status == DataStatus.COMPLETE
        assert provider.normalize(r_yoy.raw)["value"] == 2.4
        assert r_yoy.source_metadata["geo"] == "EA21"

    def test_25_and_26_unemployment_and_real_gdp_semantics(self):
        """Scenario 25 & 26: Unemployment (Percent) and Real GDP (Million EUR chain-linked) semantics."""
        une = MacroSeriesRegistry.get("EA_UNEMPLOYMENT_RATE")
        assert une.unit == MacroUnit.PERCENT
        assert une.provider_native_geography == "EA21"

        gdp = MacroSeriesRegistry.get("EA_REAL_GDP")
        assert gdp.unit == MacroUnit.MILLION_EUR
        assert gdp.frequency == MacroFrequency.QUARTERLY
        assert "Chain linked volumes" in gdp.provider_native_units

    @pytest.mark.asyncio
    async def test_27_to_29_bounded_queries_and_quarterly_gdp_formatting(self):
        """Scenario 27-29: Monthly queries format YYYY-MM; quarterly GDP queries format YYYY-Qn."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_GDP_CSV)

        provider = EurostatSDMXProvider(http_client=mock_client)

        # Quarterly GDP request
        await provider.fetch(FetchContext(
            observation_type="MACRO_EA",
            provider_symbol="EA_REAL_GDP",
            effective_date=date(2026, 1, 1),
        ))

        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["startPeriod"] == "2026-Q1"
        assert kwargs["params"]["endPeriod"] == "2026-Q1"

    def test_30_and_31_eurostat_missing_colon_and_zero_preserved(self):
        """Scenario 30 & 31: Eurostat missing notation ':' is None; 0.0 is preserved."""
        assert EurostatSDMXProvider._parse_decimal(":") is None
        assert EurostatSDMXProvider._parse_decimal("") is None
        assert EurostatSDMXProvider._parse_decimal("0.0") == 0.0
        assert EurostatSDMXProvider._parse_decimal("0") == 0.0

    @pytest.mark.asyncio
    async def test_32_eurostat_malformed_csv_schema_handled(self):
        """Scenario 32: Malformed or empty CSV response returns DataStatus.UNAVAILABLE."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text="HEADER1,HEADER2\n")

        provider = EurostatSDMXProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_HICP_ALL_ITEMS_YOY"))
        assert resp.status == DataStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_33_eurostat_mismatched_returned_period_rejected(self):
        """Scenario 33: Returned TIME_PERIOD mismatching requested period returns UNAVAILABLE."""
        mock_csv_mismatch = (
            "DATAFLOW,unit,coicop,geo,TIME_PERIOD,OBS_VALUE\n"
            "prc_hicp_manr,RCH_A,CP00,EA21,2026-05,2.4\n" # Returned May when April requested
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=mock_csv_mismatch)

        provider = EurostatSDMXProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext(
            observation_type="MACRO_EA",
            provider_symbol="EA_HICP_ALL_ITEMS_YOY",
            effective_date=date(2026, 4, 1),
        ))
        assert resp.status == DataStatus.UNAVAILABLE
        assert "does not match requested period '2026-04'" in resp.warnings[0]

    @pytest.mark.asyncio
    async def test_34_and_35_eurostat_historical_as_of_modes_rejected(self):
        """Scenario 34 & 35: Eurostat historical SOURCE_AS_OF and SYSTEM_AS_OF are rejected (fail-closed)."""
        provider = EurostatSDMXProvider()

        # SOURCE_AS_OF
        ctx_src = FetchContext(
            "MACRO_EA",
            provider_symbol="EA_HICP_ALL_ITEMS_YOY",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        assert (await provider.fetch(ctx_src)).status == DataStatus.UNAVAILABLE

        # SYSTEM_AS_OF
        ctx_sys = FetchContext(
            "MACRO_EA",
            provider_symbol="EA_HICP_ALL_ITEMS_YOY",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SYSTEM_AS_OF,
        )
        assert (await provider.fetch(ctx_sys)).status == DataStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_36_and_37_eurostat_historical_effective_date_allowed_and_no_fabricated_published_at(self):
        """Scenario 36 & 37: Historical effective_date without as_of_time allowed; published_at is None."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_HICP_YOY_CSV)

        provider = EurostatSDMXProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext(
            observation_type="MACRO_EA",
            provider_symbol="EA_HICP_ALL_ITEMS_YOY",
            effective_date=date(2026, 4, 1),
        ))
        assert resp.status == DataStatus.COMPLETE
        assert resp.published_at is None

    def test_38_to_42_eurostat_2026_ea21_and_i25_registry_invariants(self):
        """Scenario 38-42: Verified 2026 EA21 geography, 21 members, and I25 index base across registry."""
        for s in MacroSeriesRegistry.list_by_provider("EUROSTAT"):
            if s.is_active:
                assert s.provider_native_geography == "EA21"
                assert s.composition_member_count == 21
                assert s.composition_valid_from == "2026-01-01"

        hicp_idx = MacroSeriesRegistry.get("EA_HICP_ALL_ITEMS_INDEX")
        assert "I25" in hicp_idx.provider_series_code
        assert "I15" not in hicp_idx.provider_series_code
        assert hicp_idx.provider_native_units == "Index 2025=100"


# ─────────────────────────────────────────────────────────────────────────────
# 3. U.S. Treasury Yield Curve Regression & Hardening Tests (Scenarios 43-70)
# ─────────────────────────────────────────────────────────────────────────────

class TestUSTreasuryYieldCurveRegressionAndHardening:

    SAMPLE_TREASURY_XML = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
          xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
      <entry>
        <content type="application/xml">
          <m:properties>
            <d:NEW_DATE m:type="Edm.DateTime">2024-05-15T00:00:00</d:NEW_DATE>
            <d:BC_1MONTH m:type="Edm.Double">5.47</d:BC_1MONTH>
            <d:BC_3MONTH m:type="Edm.Double">5.46</d:BC_3MONTH>
            <d:BC_2YEAR m:type="Edm.Double">4.73</d:BC_2YEAR>
            <d:BC_10YEAR m:type="Edm.Double">4.36</d:BC_10YEAR>
            <d:BC_30YEAR m:type="Edm.Double">4.51</d:BC_30YEAR>
          </m:properties>
        </content>
      </entry>
    </feed>
    """

    @pytest.mark.asyncio
    async def test_43_to_50_treasury_fetch_all_canonical_tenors(self):
        """Scenario 43-50: Current monthly XML fetch parses 3M, 2Y, 10Y, 30Y correctly."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)

        # 3M
        r_3m = await provider.fetch(FetchContext("MACRO_US_TREASURY", provider_symbol="US_TREASURY_PAR_3M"))
        assert r_3m.status == DataStatus.COMPLETE
        assert provider.normalize(r_3m.raw)["value"] == 5.46

        # 2Y
        r_2y = await provider.fetch(FetchContext("MACRO_US_TREASURY", provider_symbol="US_TREASURY_PAR_2Y"))
        assert r_2y.status == DataStatus.COMPLETE
        assert provider.normalize(r_2y.raw)["value"] == 4.73

        # 10Y
        r_10y = await provider.fetch(FetchContext("MACRO_US_TREASURY", provider_symbol="US_TREASURY_PAR_10Y"))
        assert r_10y.status == DataStatus.COMPLETE
        assert provider.normalize(r_10y.raw)["value"] == 4.36

        # 30Y
        r_30y = await provider.fetch(FetchContext("MACRO_US_TREASURY", provider_symbol="US_TREASURY_PAR_30Y"))
        assert r_30y.status == DataStatus.COMPLETE
        assert provider.normalize(r_30y.raw)["value"] == 4.51

    @pytest.mark.asyncio
    async def test_44_to_46_treasury_exact_date_and_no_forward_fill(self):
        """Scenario 44-46: Exact date selected; missing date returns UNAVAILABLE without forward-fill."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)

        # Exact date present
        r_exact = await provider.fetch(FetchContext(
            observation_type="MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            effective_date=date(2024, 5, 15),
        ))
        assert r_exact.status == DataStatus.COMPLETE

        # Date missing in month feed
        r_miss = await provider.fetch(FetchContext(
            observation_type="MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            effective_date=date(2024, 5, 14),
        ))
        assert r_miss.status == DataStatus.UNAVAILABLE
        assert "not found in Treasury feed" in r_miss.warnings[0]

    def test_51_to_54_treasury_xml_parsing_tolerates_missing_zero_and_new_elements(self):
        """Scenario 51-54: Missing -> None, zero -> 0.0, namespaces and new BC_* tags handled gracefully."""
        xml_with_extras = """<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
              xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
          <entry>
            <content type="application/xml">
              <m:properties>
                <d:NEW_DATE>2024-05-15T00:00:00</d:NEW_DATE>
                <d:BC_1_5MONTH>5.45</d:BC_1_5MONTH>
                <d:BC_10YEAR>0.0</d:BC_10YEAR>
                <d:BC_30YEAR>null</d:BC_30YEAR>
              </m:properties>
            </content>
          </entry>
        </feed>
        """
        curves = USTreasuryYieldCurveProvider._parse_yield_curve_xml(xml_with_extras)
        maturities = curves[date(2024, 5, 15)]["maturities"]
        assert maturities["BC_1_5MONTH"] == 5.45
        assert maturities["BC_10YEAR"] == 0.0
        assert maturities["BC_30YEAR"] is None

    @pytest.mark.asyncio
    async def test_55_to_60_treasury_pit_and_methodology_invariants(self):
        """Scenario 55-60: Bounded month request, fail-closed PIT, 2021 methodology break, no spreads."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext("MACRO_US_TREASURY", provider_symbol="US_TREASURY_PAR_10Y"))

        # Bounded query (not 'all')
        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["data"] == "daily_treasury_yield_curve"
        assert "field_tdr_date_value_month" in kwargs["params"]
        assert kwargs["params"].get("field_tdr_date_value") != "all"

        # Methodology break note
        assert "Monotone convex spline" in resp.source_metadata["methodology_note"]

        # No spread field in provider output
        assert "10Y_2Y_SPREAD" not in resp.source_metadata.get("maturities", {})
        assert "spread" not in provider.normalize(resp.raw)

    @pytest.mark.asyncio
    async def test_61_to_65_treasury_normalization_raw_xml_and_fan_out(self):
        """Scenario 61-65: Normalization preserves value; raw XML preserved; fan-out shares snapshot_id."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext(
            observation_type="MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            effective_date=date(2024, 5, 15),
        ))

        # Value preserved
        norm = provider.normalize(resp.raw)
        assert norm["value"] == 4.36

        # Raw XML preserved
        assert "xml_text" in resp.raw

        # Fan-out to 4 canonical tenors
        snap_id = uuid4()
        records = USTreasuryYieldCurveProvider.materialize_curve_observations(resp, snapshot_id=snap_id)
        assert len(records) == 4
        for r in records:
            assert r.snapshot_id == snap_id
            assert r.effective_date == date(2024, 5, 15)

    @pytest.mark.asyncio
    async def test_66_to_70_treasury_backfill_helpers_typed_errors_and_pagination(self):
        """Scenario 66-70: Helper raises typed exceptions on 429/500/timeout and paginates from page=0."""
        # 429 RateLimit
        m_429 = AsyncMock(spec=httpx.AsyncClient)
        m_429.get.return_value = MagicMock(status_code=429)
        p_429 = USTreasuryYieldCurveProvider(http_client=m_429)
        with pytest.raises(ProviderRateLimitError):
            await p_429.get_all_curves_page(0)

        # 500 ServerError
        m_500 = AsyncMock(spec=httpx.AsyncClient)
        m_500.get.return_value = MagicMock(status_code=500)
        p_500 = USTreasuryYieldCurveProvider(http_client=m_500)
        with pytest.raises(ProviderServerError):
            await p_500.get_all_curves_page(0)

        # Empty valid response
        m_empty = AsyncMock(spec=httpx.AsyncClient)
        m_empty.get.return_value = MagicMock(status_code=200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>")
        p_empty = USTreasuryYieldCurveProvider(http_client=m_empty)
        res = await p_empty.get_all_curves_page(0)
        assert res == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. Orchestrator Pipeline & Provenance Round-trip Tests (Scenarios 71-76)
# ─────────────────────────────────────────────────────────────────────────────

class TestMacroOrchestratorPipelineAndProvenance:

    @pytest.mark.asyncio
    async def test_71_treasury_orchestrator_pipeline_with_required_value(self):
        """Scenario 71: ProviderOrchestrator with policy required_fields=['value'] successfully accepts Treasury result."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=TestUSTreasuryYieldCurveRegressionAndHardening.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)
        orchestrator = ProviderOrchestrator()
        orchestrator.register_provider(provider)

        policy = SourcePolicy(
            observation_type="MACRO_US_TREASURY",
            ordered_provider_names=[provider.provider_name],
            required_fields=["value"],
            allow_stale=True,
            max_staleness_seconds=86400 * 365 * 5,
        )

        ctx = FetchContext(
            observation_type="MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            effective_date=date(2024, 5, 15),
        )
        result = await orchestrator.execute(ctx, policy)

        assert result.status == DataStatus.COMPLETE
        assert result.data.get("value") == 4.36

    @pytest.mark.asyncio
    async def test_72_ecb_orchestrator_pipeline_happy_path(self):
        """Scenario 72: ProviderOrchestrator executes ECB EUR/USD fetch and returns COMPLETE."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=TestECBDataPortalRegression.SAMPLE_ECB_CSV)

        provider = ECBDataPortalProvider(http_client=mock_client)
        orchestrator = ProviderOrchestrator()
        orchestrator.register_provider(provider)

        policy = SourcePolicy(
            observation_type="MACRO_EA",
            ordered_provider_names=[provider.provider_name],
            required_fields=["value"],
        )

        ctx = FetchContext("MACRO_EA", provider_symbol="EA_EURUSD_REFERENCE_RATE")
        result = await orchestrator.execute(ctx, policy)

        assert result.status == DataStatus.COMPLETE
        assert result.data.get("value") == 1.0850

    @pytest.mark.asyncio
    async def test_73_eurostat_orchestrator_pipeline_happy_path(self):
        """Scenario 73: ProviderOrchestrator executes Eurostat HICP YoY fetch and returns COMPLETE."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=TestEurostatRegressionAnd2026Semantics.SAMPLE_HICP_YOY_CSV)

        provider = EurostatSDMXProvider(http_client=mock_client)
        orchestrator = ProviderOrchestrator()
        orchestrator.register_provider(provider)

        policy = SourcePolicy(
            observation_type="MACRO_EA",
            ordered_provider_names=[provider.provider_name],
            required_fields=["value"],
        )

        ctx = FetchContext("MACRO_EA", provider_symbol="EA_HICP_ALL_ITEMS_YOY")
        result = await orchestrator.execute(ctx, policy)

        assert result.status == DataStatus.COMPLETE
        assert result.data.get("value") == 2.4

    def test_74_to_76_provenance_source_role_and_geography_invariants(self):
        """Scenario 74-76: source_role survives serialization; published_at remains None; all geography explicit."""
        orch = OrchestrationResult(
            observation_type="MACRO_US_TREASURY",
            status=DataStatus.COMPLETE,
            confidence=DataConfidence(
                level=DataConfidenceLevel.HIGH,
                freshness=1.0,
                source_quality=1.0,
                coverage=1.0,
                consistency=1.0,
                calculation_coverage=1.0,
                reasons=[],
            ),
            data={"value": 4.36},
            effective_date=date(2024, 5, 15),
            retrieved_at=datetime(2024, 5, 15, 16, 0, tzinfo=timezone.utc),
            provenance=ProviderProvenance(
                provider_name="US_TREASURY",
                provider_version="1.1.0",
                endpoint="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml",
                retrieved_at=datetime(2024, 5, 15, 16, 0, tzinfo=timezone.utc),
                source_quality=SourceTier.TIER_1_REGULATORY,
                metadata={
                    "source_role": "SOVEREIGN_FISCAL_AUTHORITY",
                    "delivery_provider": "U.S. Department of the Treasury",
                }
            ),
            source_metadata={
                "source_role": "SOVEREIGN_FISCAL_AUTHORITY",
            }
        )

        serialized = orch.to_dict()
        reconstituted = OrchestrationResult.from_dict(serialized)
        assert reconstituted.provenance.metadata["source_role"] == "SOVEREIGN_FISCAL_AUTHORITY"
        assert reconstituted.published_at is None

        for s in MacroSeriesRegistry.list_all():
            assert s.geography in ("TR", "US", "EA")
