"""
backend/tests/test_ecb_eurostat_treasury.py
=============================================
Comprehensive Unit and Point-in-Time Semantic Hardening Tests for 2026 Euro Area & U.S. Treasury Macro Layer.

Coverage:
    - Euro Area 2026 (Bulgaria accession, EA21 composition, HICP 2025=100 / I25 reference base, ECOICOP v2, Quarterly GDP period formatting, period mismatch validation)
    - ECB Policy Rates (DFR & MRO EVENT_DRIVEN frequency, None expected release interval, persistent freshness until next decision, €STR & EUR/USD daily benchmarks)
    - U.S. Treasury (No silent 10Y default, value normalization fix, raw XML preservation, curve fan-out to 3M/2Y/10Y/30Y sharing snapshot_id, typed backfill errors)
    - Cross-layer invariants (Explicit geography, missing != 0, zero network calls via pytest-socket)
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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
# 1. Eurostat 2026 Composition & HICP Reference Base (Scenarios 1-13)
# ─────────────────────────────────────────────────────────────────────────────

class TestEurostat2026SemanticHardening:

    def test_01_to_03_euro_area_2026_composition_is_ea21(self):
        """Scenario 1-3: Bulgaria joined 2026-01-01; canonical Eurostat series use EA21 composition."""
        hicp_def = MacroSeriesRegistry.get("EA_HICP_ALL_ITEMS_INDEX")
        assert hicp_def.provider_native_geography == "EA21"
        assert hicp_def.composition_member_count == 21
        assert hicp_def.composition_valid_from == "2026-01-01"
        assert "EA20" not in hicp_def.provider_series_code
        assert "EA21" in hicp_def.provider_series_code

        gdp_def = MacroSeriesRegistry.get("EA_REAL_GDP")
        assert gdp_def.provider_native_geography == "EA21"
        assert gdp_def.composition_member_count == 21
        assert "EA21" in gdp_def.provider_series_code

    def test_04_to_08_hicp_2025_reference_base_and_ecoicop_v2(self):
        """Scenario 4-8: HICP index uses 2025=100 (I25) and ECOICOP v2, separate from YoY."""
        hicp_idx = MacroSeriesRegistry.get("EA_HICP_ALL_ITEMS_INDEX")
        assert hicp_idx.provider_native_units == "Index 2025=100"
        assert "I25" in hicp_idx.provider_series_code
        assert "I15" not in hicp_idx.provider_series_code
        assert "CP00" in hicp_idx.provider_series_code

        hicp_yoy = MacroSeriesRegistry.get("EA_HICP_ALL_ITEMS_YOY")
        assert hicp_yoy.unit == MacroUnit.PERCENT
        assert "RCH_A" in hicp_yoy.provider_series_code
        assert "EA21" in hicp_yoy.provider_series_code

    def test_09_to_11_period_formatter_monthly_quarterly_annual(self):
        """Scenario 9-11: Frequency-aware period formatter produces exact SDMX TIME_PERIOD strings."""
        dt_q1 = date(2026, 2, 15)
        assert EurostatSDMXProvider._format_period(dt_q1, MacroFrequency.QUARTERLY) == "2026-Q1"

        dt_q3 = date(2026, 8, 26)
        assert EurostatSDMXProvider._format_period(dt_q3, MacroFrequency.QUARTERLY) == "2026-Q3"

        dt_m = date(2026, 4, 1)
        assert EurostatSDMXProvider._format_period(dt_m, MacroFrequency.MONTHLY) == "2026-04"

        dt_a = date(2026, 1, 1)
        assert EurostatSDMXProvider._format_period(dt_a, MacroFrequency.ANNUAL) == "2026"

    @pytest.mark.asyncio
    async def test_12_returned_wrong_time_period_rejected(self):
        """Scenario 12: If returned TIME_PERIOD mismatches requested effective period, returns UNAVAILABLE."""
        # Request 2026-Q1 but response returns 2026-Q2
        mock_csv_mismatch = (
            "DATAFLOW,unit,s_adj,na_item,geo,TIME_PERIOD,OBS_VALUE\n"
            "namq_10_gdp,CLV10_MNAC,SCA,B1GQ,EA21,2026-Q2,3100000.0\n"
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=mock_csv_mismatch)

        provider = EurostatSDMXProvider(http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_EA",
            provider_symbol="EA_REAL_GDP",
            effective_date=date(2026, 1, 1), # Requesting Q1
        )
        resp = await provider.fetch(ctx)
        assert resp.status == DataStatus.UNAVAILABLE
        assert "does not match requested period '2026-Q1'" in resp.warnings[0]


# ─────────────────────────────────────────────────────────────────────────────
# 2. ECB Policy Rate Frequency & Freshness Semantics (Scenarios 14-20)
# ─────────────────────────────────────────────────────────────────────────────

class TestECBPolicyRateSemantics:

    def test_14_to_17_dfr_and_mro_are_event_driven(self):
        """Scenario 14-17: DFR & MRO are classified as EVENT_DRIVEN with expected_release_interval_days = None."""
        dfr_def = MacroSeriesRegistry.get("EA_ECB_DEPOSIT_FACILITY_RATE")
        assert dfr_def.frequency == MacroFrequency.EVENT_DRIVEN
        assert dfr_def.expected_release_interval_days is None

        mro_def = MacroSeriesRegistry.get("EA_ECB_MAIN_REFINANCING_RATE")
        assert mro_def.frequency == MacroFrequency.EVENT_DRIVEN
        assert mro_def.expected_release_interval_days is None

    def test_18_old_policy_observation_remains_fresh_until_next_decision(self):
        """Scenario 18: An event-driven policy rate unchanged for 60 days retains freshness = 1.0 (not stale)."""
        conf = ConfidenceAssessmentService.assess(
            source_tier=SourceTier.TIER_1_REGULATORY,
            data_status=DataStatus.COMPLETE,
            effective_date=date(2024, 3, 1),
            published_at=None,
            observed_at=datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
            retrieved_at=datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
            as_of_time=datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc), # 61 days later
            required_fields=["value"],
            optional_fields=[],
            present_fields=["value"],
            max_staleness_days=None, # Event-driven series
        )
        assert conf.freshness == 1.0
        assert conf.level == DataConfidenceLevel.HIGH
        assert not any("stale" in r.lower() for r in conf.reasons)

    def test_19_and_20_estr_and_eurusd_remain_business_daily(self):
        """Scenario 19 & 20: €STR and EUR/USD benchmarks remain BUSINESS_DAILY with 1-day release intervals."""
        estr_def = MacroSeriesRegistry.get("EA_ESTR")
        assert estr_def.frequency == MacroFrequency.BUSINESS_DAILY
        assert estr_def.expected_release_interval_days == 1

        fx_def = MacroSeriesRegistry.get("EA_EURUSD_REFERENCE_RATE")
        assert fx_def.frequency == MacroFrequency.BUSINESS_DAILY
        assert fx_def.expected_release_interval_days == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. U.S. Treasury Yield Curve Provider Hardening (Scenarios 21-33)
# ─────────────────────────────────────────────────────────────────────────────

class TestUSTreasuryYieldCurveHardening:

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
    async def test_21_to_23_fetch_and_normalize_value_loss_fixed(self):
        """Scenario 21-23: Fetched Treasury 10Y & 2Y preserve 'value' deterministically through normalize."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)

        # 10Y fetch
        ctx_10y = FetchContext(observation_type="MACRO_US_TREASURY", provider_symbol="US_TREASURY_PAR_10Y")
        resp_10y = await provider.fetch(ctx_10y)
        assert resp_10y.status == DataStatus.COMPLETE
        norm_10y = provider.normalize(resp_10y.raw)
        assert norm_10y["value"] == 4.36
        assert norm_10y["target_field"] == "BC_10YEAR"

        # 2Y fetch
        ctx_2y = FetchContext(observation_type="MACRO_US_TREASURY", provider_symbol="US_TREASURY_PAR_2Y")
        resp_2y = await provider.fetch(ctx_2y)
        assert resp_2y.status == DataStatus.COMPLETE
        norm_2y = provider.normalize(resp_2y.raw)
        assert norm_2y["value"] == 4.73

    @pytest.mark.asyncio
    async def test_24_missing_provider_symbol_fails_fast_no_silent_10y(self):
        """Scenario 24: Missing provider symbol does NOT silently default to 10Y."""
        provider = USTreasuryYieldCurveProvider()
        ctx = FetchContext(observation_type="MACRO_US_TREASURY", provider_symbol="")
        resp = await provider.fetch(ctx)
        assert resp.status == DataStatus.UNAVAILABLE
        assert "No Treasury maturity" in resp.warnings[0]

    @pytest.mark.asyncio
    async def test_25_to_29_raw_xml_preserved_and_curve_fan_out(self):
        """Scenario 25-29: Raw XML preserved; materialize_curve_observations fans out 3M/2Y/10Y/30Y sharing snapshot_id."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, text=self.SAMPLE_TREASURY_XML)

        provider = USTreasuryYieldCurveProvider(http_client=mock_client)
        resp = await provider.fetch(FetchContext(
            observation_type="MACRO_US_TREASURY",
            provider_symbol="US_TREASURY_PAR_10Y",
            effective_date=date(2024, 5, 15),
        ))

        # Check raw XML snapshot payload
        assert "xml_text" in resp.raw
        assert "<d:BC_10YEAR" in resp.raw["xml_text"]

        # Fan-out to 4 canonical tenors
        shared_snapshot_id = uuid4()
        records = USTreasuryYieldCurveProvider.materialize_curve_observations(resp, snapshot_id=shared_snapshot_id)

        assert len(records) == 4
        keys = {r.series_key: r.value for r in records}
        assert keys["US_TREASURY_PAR_3M"] == 5.46
        assert keys["US_TREASURY_PAR_2Y"] == 4.73
        assert keys["US_TREASURY_PAR_10Y"] == 4.36
        assert keys["US_TREASURY_PAR_30Y"] == 4.51

        for r in records:
            assert r.snapshot_id == shared_snapshot_id
            assert r.effective_date == date(2024, 5, 15)
            assert r.unit == MacroUnit.PERCENT
            assert r.frequency == MacroFrequency.BUSINESS_DAILY

    @pytest.mark.asyncio
    async def test_30_and_31_typed_backfill_errors(self):
        """Scenario 30 & 31: Helper raises typed exceptions on HTTP error and returns [] on valid empty page."""
        # 429 RateLimit
        m_429 = AsyncMock(spec=httpx.AsyncClient)
        m_429.get.return_value = MagicMock(status_code=429)
        p_429 = USTreasuryYieldCurveProvider(http_client=m_429)
        with pytest.raises(ProviderRateLimitError):
            await p_429.get_all_curves_page(0)

        # Valid empty page
        m_empty = AsyncMock(spec=httpx.AsyncClient)
        m_empty.get.return_value = MagicMock(status_code=200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>")
        p_empty = USTreasuryYieldCurveProvider(http_client=m_empty)
        res = await p_empty.get_all_curves_page(0)
        assert res == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cross-Layer Invariants (Scenarios 34-40)
# ─────────────────────────────────────────────────────────────────────────────

class TestMacroCrossLayerHardening:

    def test_34_to_36_geography_explicit_and_missing_not_zero(self):
        """Scenario 34-36: Explicit geography across all series; missing observations are None."""
        for s in MacroSeriesRegistry.list_all():
            assert s.geography in ("TR", "US", "EA")
            if s.provider == "EUROSTAT" and s.is_active:
                assert s.provider_native_geography == "EA21"

        assert EurostatSDMXProvider._parse_decimal(":") is None
        assert EurostatSDMXProvider._parse_decimal("0.0") == 0.0
        assert ECBDataPortalProvider._parse_decimal(".") is None
        assert ECBDataPortalProvider._parse_decimal("0.0") == 0.0
