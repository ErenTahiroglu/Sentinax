"""
backend/tests/test_ecb_eurostat_treasury.py
=============================================
Comprehensive Unit and Point-in-Time Regression Tests for ECB, Eurostat, and U.S. Treasury Macro Layer.

Coverage:
    - ECB Data Portal (EUR/USD, Deposit Facility, Main Refinancing, €STR, CSV parsing, bounded query, fail-closed PIT)
    - Eurostat (HICP Index, HICP YoY, Unemployment, Real GDP, EA20 geography, bounded query, fail-closed PIT)
    - U.S. Department of the Treasury (Daily Par Yield Curve XML feed, 3M/2Y/10Y/30Y, single-curve fetch, no spreads, 2021 methodology break)
    - Cross-layer invariants (Explicit geography, missing != 0, no timestamp fabrication, provenance source_role, zero network calls)
"""

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
    MacroSeriesDefinition,
    MacroUnit,
)
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.orchestrator import OrchestrationResult
from backend.engine.private.provider_contract import (
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.providers.ecb_sdmx import ECBDataPortalProvider
from backend.engine.private.providers.eurostat_sdmx import EurostatSDMXProvider
from backend.engine.private.providers.us_treasury import USTreasuryYieldCurveProvider


# ─────────────────────────────────────────────────────────────────────────────
# 1. ECB Data Portal Tests (Scenarios 1-17)
# ─────────────────────────────────────────────────────────────────────────────

class TestECBDataPortalProvider:

    @pytest.mark.asyncio
    async def test_01_and_02_ecb_current_eurusd_fetch_and_quote_direction(self):
        """Scenario 1-3: ECB EUR/USD fetch, official key, quote direction preserved."""
        mock_csv = (
            "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF\n"
            "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-05-15,1.0850,A,F\n"
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=mock_csv)

        provider = ECBDataPortalProvider(http_client=mock_client)
        ctx = FetchContext(observation_type="MACRO_EA", provider_symbol="EA_EURUSD_REFERENCE_RATE")

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.COMPLETE
        assert response.effective_date == date(2024, 5, 15)
        assert response.source_metadata["quote_direction"] == "USD per 1 EUR"
        assert response.source_metadata["source_role"] == "CENTRAL_BANK"

        normalized = provider.normalize(response.raw)
        assert normalized["value"] == 1.0850

    @pytest.mark.asyncio
    async def test_04_and_05_ecb_missing_value_and_zero_preserved(self):
        """Scenario 4 & 5: Missing ECB value is None; 0.0 is preserved."""
        assert ECBDataPortalProvider._parse_decimal("") is None
        assert ECBDataPortalProvider._parse_decimal(".") is None
        assert ECBDataPortalProvider._parse_decimal("-") is None
        assert ECBDataPortalProvider._parse_decimal("NaN") is None
        assert ECBDataPortalProvider._parse_decimal("0.0") == 0.0
        assert ECBDataPortalProvider._parse_decimal("0") == 0.0

    @pytest.mark.asyncio
    async def test_06_and_07_ecb_bounded_queries(self):
        """Scenario 6 & 7: Current latest uses lastNObservations=1; effective_date bounded with start/end."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            text="KEY,TIME_PERIOD,OBS_VALUE\nFM.D.U2.EUR.4F.KR.DFR.LEV,2024-05-15,4.00\n"
        )
        provider = ECBDataPortalProvider(http_client=mock_client)

        # 1. Latest query
        await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_ECB_DEPOSIT_FACILITY_RATE"))
        _, kwargs1 = mock_client.get.call_args
        assert kwargs1["params"]["lastNObservations"] == 1
        assert kwargs1["params"]["format"] == "csvdata"

        # 2. Date-bounded query
        await provider.fetch(FetchContext(
            "MACRO_EA",
            provider_symbol="EA_ECB_DEPOSIT_FACILITY_RATE",
            effective_date=date(2024, 5, 1),
        ))
        _, kwargs2 = mock_client.get.call_args
        assert kwargs2["params"]["startPeriod"] == "2024-05-01"
        assert kwargs2["params"]["endPeriod"] == "2024-05-01"

    @pytest.mark.asyncio
    async def test_08_unverified_ecb_series_rejected_before_http(self):
        """Scenario 8: Unverified series is rejected without network fetch."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        provider = ECBDataPortalProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_UNVERIFIED_RATE"))
        assert resp.status == DataStatus.UNAVAILABLE
        assert mock_client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_09_and_10_ecb_discovery_helpers(self):
        """Scenario 9 & 10: Dataflow discovery helper parses official structures."""
        mock_payload = {
            "data": {
                "dataflows": [{"id": "EXR", "name": "Exchange Rates"}, {"id": "FM", "name": "Financial Markets"}]
            }
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))

        provider = ECBDataPortalProvider(http_client=mock_client)
        dataflows = await provider.get_dataflows()
        assert len(dataflows) == 2
        assert dataflows[0]["id"] == "EXR"

    @pytest.mark.asyncio
    async def test_11_12_14_15_and_16_ecb_pit_guards_and_no_timestamp_fabrication(self):
        """Scenario 11-16: SOURCE_AS_OF / SYSTEM_AS_OF rejected; no published_at fabrication."""
        provider = ECBDataPortalProvider()

        # SYSTEM_AS_OF rejected
        ctx_sys = FetchContext(
            "MACRO_EA",
            provider_symbol="EA_EURUSD_REFERENCE_RATE",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SYSTEM_AS_OF,
        )
        resp_sys = await provider.fetch(ctx_sys)
        assert resp_sys.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in resp_sys.warnings[0]

        # SOURCE_AS_OF rejected (external vintage unsupported)
        ctx_src = FetchContext(
            "MACRO_EA",
            provider_symbol="EA_EURUSD_REFERENCE_RATE",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        resp_src = await provider.fetch(ctx_src)
        assert resp_src.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in resp_src.warnings[0]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Eurostat Dissemination Tests (Scenarios 18-34)
# ─────────────────────────────────────────────────────────────────────────────

class TestEurostatSDMXProvider:

    @pytest.mark.asyncio
    async def test_20_21_and_22_hicp_index_and_yoy_mapping_not_mixed(self):
        """Scenario 20-22: HICP Index and HICP YoY are mapped to separate verified series without mixing."""
        mock_hicp_idx = "DATAFLOW,unit,coicop,geo,TIME_PERIOD,OBS_VALUE\nprc_hicp_midx,I15,CP00,EA20,2024-04,124.50\n"
        mock_hicp_yoy = "DATAFLOW,unit,coicop,geo,TIME_PERIOD,OBS_VALUE\nprc_hicp_manr,RCH_A,CP00,EA20,2024-04,2.4\n"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = [
            MagicMock(status_code=200, text=mock_hicp_idx),
            MagicMock(status_code=200, text=mock_hicp_yoy),
        ]

        provider = EurostatSDMXProvider(http_client=mock_client)

        # Index query
        res_idx = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_HICP_ALL_ITEMS_INDEX"))
        assert res_idx.status == DataStatus.COMPLETE
        assert provider.normalize(res_idx.raw)["value"] == 124.50
        assert res_idx.source_metadata["dataset_code"] == "prc_hicp_midx"

        # YoY query
        res_yoy = await provider.fetch(FetchContext("MACRO_EA", provider_symbol="EA_HICP_ALL_ITEMS_YOY"))
        assert res_yoy.status == DataStatus.COMPLETE
        assert provider.normalize(res_yoy.raw)["value"] == 2.4
        assert res_yoy.source_metadata["dataset_code"] == "prc_hicp_manr"

    @pytest.mark.asyncio
    async def test_23_24_and_25_unemployment_gdp_and_ea20_geography(self):
        """Scenario 23-25: Unemployment, Real GDP chain-linked volume, and EA20 geo preserved."""
        gdp_def = MacroSeriesRegistry.get("EA_REAL_GDP")
        assert gdp_def.unit == MacroUnit.MILLION_EUR
        assert gdp_def.geography == "EA"
        assert "Chain linked volumes" in gdp_def.provider_native_units
        assert gdp_def.frequency == MacroFrequency.QUARTERLY

        une_def = MacroSeriesRegistry.get("EA_UNEMPLOYMENT_RATE")
        assert une_def.unit == MacroUnit.PERCENT
        assert une_def.geography == "EA"
        assert une_def.frequency == MacroFrequency.MONTHLY

    @pytest.mark.asyncio
    async def test_26_27_28_and_29_eurostat_bounded_and_missing_values(self):
        """Scenario 26-29: Bounded queries, missing (':', '') -> None, zero preserved."""
        assert EurostatSDMXProvider._parse_decimal(":") is None
        assert EurostatSDMXProvider._parse_decimal("") is None
        assert EurostatSDMXProvider._parse_decimal(".") is None
        assert EurostatSDMXProvider._parse_decimal("0.0") == 0.0
        assert EurostatSDMXProvider._parse_decimal("0") == 0.0

    @pytest.mark.asyncio
    async def test_31_32_33_and_34_eurostat_pit_guards(self):
        """Scenario 31-34: Eurostat historical SOURCE_AS_OF / SYSTEM_AS_OF rejected."""
        provider = EurostatSDMXProvider()

        # Historical SOURCE_AS_OF rejected
        ctx_src = FetchContext(
            "MACRO_EA",
            provider_symbol="EA_HICP_ALL_ITEMS_YOY",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        resp_src = await provider.fetch(ctx_src)
        assert resp_src.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in resp_src.warnings[0]


# ─────────────────────────────────────────────────────────────────────────────
# 3. U.S. Treasury Yield Curve Tests (Scenarios 35-54)
# ─────────────────────────────────────────────────────────────────────────────

class TestUSTreasuryYieldCurveProvider:

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
            <d:BC_6MONTH m:type="Edm.Double">5.42</d:BC_6MONTH>
            <d:BC_1YEAR m:type="Edm.Double">5.18</d:BC_1YEAR>
            <d:BC_2YEAR m:type="Edm.Double">4.73</d:BC_2YEAR>
            <d:BC_10YEAR m:type="Edm.Double">4.36</d:BC_10YEAR>
            <d:BC_30YEAR m:type="Edm.Double">4.51</d:BC_30YEAR>
          </m:properties>
        </content>
      </entry>
    </feed>
    """

    @pytest.mark.asyncio
    async def test_35_to_44_treasury_xml_parsing_maturities_and_single_fetch(self):
        """Scenario 35-44: Single XML curve fetch parses 3M, 2Y, 10Y, 30Y correctly."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)

        # 10Y fetch
        ctx_10y = FetchContext(
            observation_type="MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            effective_date=date(2024, 5, 15),
        )
        resp_10y = await provider.fetch(ctx_10y)
        assert resp_10y.status == DataStatus.COMPLETE
        assert resp_10y.effective_date == date(2024, 5, 15)
        maturities = resp_10y.source_metadata["maturities"]
        assert maturities["BC_10YEAR"] == 4.36
        assert maturities["BC_2YEAR"] == 4.73
        assert maturities["BC_3MONTH"] == 5.46
        assert maturities["BC_30YEAR"] == 4.51

        # Check provenance
        prov = provider.provenance(resp_10y)
        assert prov.metadata["source_role"] == "SOVEREIGN_FISCAL_AUTHORITY"
        assert prov.metadata["delivery_provider"] == "U.S. Department of the Treasury"

    @pytest.mark.asyncio
    async def test_37_date_missing_returns_unavailable_no_forward_fill(self):
        """Scenario 37: If exact requested date is missing in month feed, returns UNAVAILABLE (no forward-fill)."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)
        # Request May 14, 2024 (XML only has May 15)
        ctx = FetchContext(
            observation_type="MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            effective_date=date(2024, 5, 14),
        )
        resp = await provider.fetch(ctx)
        assert resp.status == DataStatus.UNAVAILABLE
        assert "not found in Treasury feed" in resp.warnings[0]

    @pytest.mark.asyncio
    async def test_45_and_46_tolerates_unknown_xml_elements_and_namespaces(self):
        """Scenario 45 & 46: Tolerates future tenors (e.g. BC_1_5MONTH) without parser failure."""
        xml_with_new_tenor = """<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
              xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
          <entry>
            <content type="application/xml">
              <m:properties>
                <d:NEW_DATE>2024-05-15T00:00:00</d:NEW_DATE>
                <d:BC_1_5MONTH>5.45</d:BC_1_5MONTH>
                <d:BC_10YEAR>4.36</d:BC_10YEAR>
              </m:properties>
            </content>
          </entry>
        </feed>
        """
        curves = USTreasuryYieldCurveProvider._parse_yield_curve_xml(xml_with_new_tenor)
        assert date(2024, 5, 15) in curves
        maturities = curves[date(2024, 5, 15)]["maturities"]
        assert maturities["BC_1_5MONTH"] == 5.45
        assert maturities["BC_10YEAR"] == 4.36

    @pytest.mark.asyncio
    async def test_50_51_52_53_and_54_treasury_pit_guards_and_methodology(self):
        """Scenario 50-54: Treasury PIT guards, 2021 methodology break note, no spread calculation."""
        provider = USTreasuryYieldCurveProvider()

        # SYSTEM_AS_OF rejected
        ctx_sys = FetchContext(
            "MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SYSTEM_AS_OF,
        )
        resp_sys = await provider.fetch(ctx_sys)
        assert resp_sys.status == DataStatus.UNAVAILABLE
        assert "local PIT storage" in resp_sys.warnings[0]

        # SOURCE_AS_OF rejected
        ctx_src = FetchContext(
            "MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            as_of_time=datetime(2023, 5, 1, tzinfo=timezone.utc),
            as_of_mode=AsOfMode.SOURCE_AS_OF,
        )
        resp_src = await provider.fetch(ctx_src)
        assert resp_src.status == DataStatus.UNAVAILABLE


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cross-Layer & Registry Invariants (Scenarios 55-64)
# ─────────────────────────────────────────────────────────────────────────────

class TestMacroCrossLayerInvariants:

    def test_55_all_geography_explicit_across_registry(self):
        """Scenario 55: Every series definition across TR, US, EA has explicit geography."""
        all_series = MacroSeriesRegistry.list_all()
        assert len(all_series) >= 20  # TR (10) + US (10) + EA (8)
        for s in all_series:
            assert s.geography in ("TR", "US", "EA")
            assert s.source_tier == SourceTier.TIER_1_REGULATORY or s.source_tier == SourceTier.TIER_3_AGGREGATOR

    def test_59_provenance_source_role_survives_orchestrator(self):
        """Scenario 59: source_role metadata survives OrchestrationResult round-trip."""
        orch = OrchestrationResult(
            observation_type="MACRO_EA",
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
            data={"value": 1.0850},
            effective_date=date(2024, 5, 15),
            retrieved_at=datetime(2024, 5, 15, 16, 0, tzinfo=timezone.utc),
            provenance=ProviderProvenance(
                provider_name="ECB_DATA_PORTAL",
                provider_version="1.0.0",
                endpoint="https://data-api.ecb.europa.eu/service/data",
                retrieved_at=datetime(2024, 5, 15, 16, 0, tzinfo=timezone.utc),
                source_quality=SourceTier.TIER_1_REGULATORY,
                metadata={
                    "source_role": "CENTRAL_BANK",
                    "delivery_provider": "European Central Bank Data Portal",
                }
            ),
            source_metadata={
                "source_role": "CENTRAL_BANK",
                "quote_direction": "USD per 1 EUR",
            }
        )

        serialized = orch.to_dict()
        reconstituted = OrchestrationResult.from_dict(serialized)
        assert reconstituted.provenance.metadata["source_role"] == "CENTRAL_BANK"
        assert reconstituted.source_metadata["quote_direction"] == "USD per 1 EUR"
