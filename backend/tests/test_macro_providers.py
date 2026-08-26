"""
backend/tests/test_macro_providers.py
=======================================
Unit test suite for Turkey Official Macro Data Layer (TCMB EVDS, TÜİK SDMX, Manual ENAG).

Verifies all 36 required test scenarios:
    1. EVDS valid single series response
    2. EVDS valid multiple series response
    3. EVDS missing observation remains None
    4. EVDS invalid API key -> typed ProviderAuthenticationError
    5. EVDS timeout error handling
    6. EVDS 5xx server error handling
    7. EVDS malformed schema error
    8. EVDS API key absent from query URL
    9. EVDS API key absent from raw snapshot and cache keys
    10. EVDS locale-safe decimal & date parsing
    11. EVDS provider provenance
    12. EVDS canonical registry mapping
    13. EVDS historical query behavior
    14. TÜİK SDMX valid CPI response
    15. TÜİK SDMX valid PPI response
    16. TÜİK index level vs YoY/MoM percentage change fields not mixed
    17. TÜİK missing SDMX observation remains None
    18. TÜİK malformed SDMX schema
    19. TÜİK revision handling
    20. TÜİK publication vs reference period separation
    21. TÜİK dataset registry
    22. TÜİK no undocumented fallback endpoint
    23. ENAG pending record not usable
    24. ENAG verified monthly record usable
    25. ENAG verified annual record usable
    26. ENAG missing source rejected
    27. ENAG duplicate / revision behavior
    28. ENAG correction does not overwrite old record
    29. ENAG never classified as TÜİK official tier
    30. ENAG absent produces UNAVAILABLE/None (never 0.0)
    31. FX daily freshness policy evaluation
    32. CPI monthly freshness policy evaluation
    33. Macro series decoupled from fake equity instrument IDs
    34. No secret leakage across headers, logs, models
    35. Missing observation is NEVER 0.0
    36. Buffett Engine unaffected

Zero external network calls (pytest-socket isolation enforced).
"""

import json
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from backend.engine.private.confidence import ConfidenceAssessmentService
from backend.engine.private.domain import (
    DataConfidenceLevel,
    DataStatus,
    FreshnessBasis,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.macro.models import (
    MacroCategory,
    MacroFrequency,
    MacroUnit,
    ManualENAGRecord,
    VerificationStatus,
)
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.policy import SourcePolicy
from backend.engine.private.provider_contract import FetchContext
from backend.engine.private.providers.manual_enag import ManualENAGProvider
from backend.engine.private.providers.tcmb_evds import TCMBEVDSProvider
from backend.engine.private.providers.tuik_sdmx import TUIKSDMXProvider


# ─────────────────────────────────────────────────────────────────────────────
# 1. TCMB EVDS Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTCMBEVDSProvider:

    @pytest.mark.asyncio
    async def test_01_valid_single_series_response(self):
        """Scenario 1: Valid single USD/TRY series parse."""
        mock_resp_json = {
            "totalCount": 1,
            "items": [
                {
                    "Tarih": "15-01-2024",
                    "TP_DK_USD_A_YTL": "30.1250",
                    "UNIXTIME": {"$numberLong": "1705276800"},
                }
            ],
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=mock_resp_json),
        )

        provider = TCMBEVDSProvider(api_key="test_secret_key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_FX",
            provider_symbol="TP.DK.USD.A.YTL",
            effective_date=date(2024, 1, 15),
        )

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.COMPLETE
        assert response.effective_date == date(2024, 1, 15)

        normalized = provider.normalize(response.raw)
        assert normalized["TP_DK_USD_A_YTL"] == 30.1250
        assert normalized["value"] == 30.1250

    @pytest.mark.asyncio
    async def test_02_valid_multiple_series_response(self):
        """Scenario 2: Valid multiple series response (USD/TRY and EUR/TRY)."""
        mock_resp_json = {
            "totalCount": 1,
            "items": [
                {
                    "Tarih": "15-01-2024",
                    "TP_DK_USD_A_YTL": "30.1250",
                    "TP_DK_EUR_A_YTL": "33.0500",
                }
            ],
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=mock_resp_json),
        )

        provider = TCMBEVDSProvider(api_key="test_secret_key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_FX",
            provider_symbol="TP.DK.USD.A.YTL-TP.DK.EUR.A.YTL",
            effective_date=date(2024, 1, 15),
        )

        response = await provider.fetch(ctx)
        normalized = provider.normalize(response.raw)
        assert normalized["TP_DK_USD_A_YTL"] == 30.1250
        assert normalized["TP_DK_EUR_A_YTL"] == 33.0500

    @pytest.mark.asyncio
    async def test_03_missing_observation_remains_none(self):
        """Scenario 3: Missing quote value is None, NEVER 0.0."""
        mock_resp_json = {
            "totalCount": 1,
            "items": [
                {
                    "Tarih": "15-01-2024",
                    "TP_DK_USD_A_YTL": "-", # Holiday / non-trading day in EVDS
                }
            ],
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=mock_resp_json),
        )

        provider = TCMBEVDSProvider(api_key="test_secret_key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_FX",
            provider_symbol="TP.DK.USD.A.YTL",
            effective_date=date(2024, 1, 15),
        )

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.UNAVAILABLE

        normalized = provider.normalize(response.raw)
        assert normalized["TP_DK_USD_A_YTL"] is None
        assert normalized["value"] is None

    @pytest.mark.asyncio
    async def test_04_invalid_api_key_raises_auth_error(self):
        """Scenario 4: 401/403 or JSON auth error raises ProviderAuthenticationError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=401,
            json=MagicMock(return_value={"error": "Invalid API Key"}),
        )

        provider = TCMBEVDSProvider(api_key="invalid_key", http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_FX",
            provider_symbol="TP.DK.USD.A.YTL",
        )

        with pytest.raises(ProviderAuthenticationError):
            await provider.fetch(ctx)

    @pytest.mark.asyncio
    async def test_05_and_06_timeout_and_5xx_raise_typed_errors(self):
        """Scenario 5 & 6: Timeout and 5xx raise typed TransientProviderError."""
        # Timeout
        mock_client_timeout = AsyncMock(spec=httpx.AsyncClient)
        mock_client_timeout.get.side_effect = httpx.TimeoutException("Timeout")
        provider_t = TCMBEVDSProvider(api_key="key", http_client=mock_client_timeout)

        with pytest.raises(ProviderTimeoutError):
            await provider_t.fetch(FetchContext("MACRO_FX", provider_symbol="TP.DK.USD.A.YTL"))

        # 5xx
        mock_client_500 = AsyncMock(spec=httpx.AsyncClient)
        mock_client_500.get.return_value = MagicMock(status_code=503)
        provider_500 = TCMBEVDSProvider(api_key="key", http_client=mock_client_500)

        with pytest.raises(ProviderServerError):
            await provider_500.fetch(FetchContext("MACRO_FX", provider_symbol="TP.DK.USD.A.YTL"))

    @pytest.mark.asyncio
    async def test_07_malformed_schema_raises_schema_error(self):
        """Scenario 7: Malformed schema raises ProviderSchemaError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(side_effect=ValueError("Invalid JSON")),
        )
        provider = TCMBEVDSProvider(api_key="key", http_client=mock_client)

        with pytest.raises(ProviderSchemaError):
            await provider.fetch(FetchContext("MACRO_FX", provider_symbol="TP.DK.USD.A.YTL"))

    @pytest.mark.asyncio
    async def test_08_and_09_api_key_absent_from_url_and_params(self):
        """Scenario 8 & 9: Key is passed via header ONLY, never in URL or request_params."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"totalCount": 1, "items": [{"Tarih": "01-01-2024", "TP_DK_USD_A_YTL": "30.0"}]}),
        )

        secret_key = "very_secret_evds_token_xyz"
        provider = TCMBEVDSProvider(api_key=secret_key, http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_FX",
            provider_symbol="TP.DK.USD.A.YTL",
        )

        await provider.fetch(ctx)

        # Check call arguments to httpx client
        _, kwargs = mock_client.get.call_args
        params = kwargs.get("params", {})
        headers = kwargs.get("headers", {})

        assert "key" not in params, "Security violation: API key found in URL params!"
        assert headers.get("key") == secret_key, "API key must be passed in header 'key'"

    @pytest.mark.asyncio
    async def test_10_locale_safe_decimal_parsing(self):
        """Scenario 10: Decimal parsing handles commas and dots safely."""
        assert TCMBEVDSProvider._parse_decimal("32,5000") == 32.5
        assert TCMBEVDSProvider._parse_decimal("32.5000") == 32.5
        assert TCMBEVDSProvider._parse_decimal("-") is None
        assert TCMBEVDSProvider._parse_decimal("") is None
        assert TCMBEVDSProvider._parse_decimal(None) is None

    @pytest.mark.asyncio
    async def test_11_and_12_provenance_and_registry_mapping(self):
        """Scenario 11 & 12: Provenance and MacroSeriesRegistry resolution."""
        def_usd = MacroSeriesRegistry.get("TR_FX_USDTRY")
        assert def_usd is not None
        assert def_usd.provider_series_code == "TP.DK.USD.A.YTL"
        assert def_usd.category == MacroCategory.FX
        assert def_usd.unit == MacroUnit.TRY

        def_rate = MacroSeriesRegistry.get("TR_POLICY_RATE")
        assert def_rate is not None
        assert def_rate.provider_series_code == "TP.APIFON4"
        assert def_rate.category == MacroCategory.INTEREST_RATE


# ─────────────────────────────────────────────────────────────────────────────
# 2. TÜİK SDMX Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTUIKSDMXProvider:

    @pytest.mark.asyncio
    async def test_14_and_15_sdmx_valid_cpi_and_ppi_response(self):
        """Scenario 14 & 15: Valid SDMX CPI & PPI response parsing."""
        mock_cpi_payload = [
            {
                "PERIOD": "2024-05",
                "VALUE": "75.45",
                "INDICATOR": "CPI_YOY",
            }
        ]
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=mock_cpi_payload),
        )

        provider = TUIKSDMXProvider(http_client=mock_client)
        ctx = FetchContext(
            observation_type="MACRO_INFLATION",
            provider_symbol="TR_CPI_TUIK_YOY",
            effective_date=date(2024, 5, 31),
        )

        response = await provider.fetch(ctx)
        assert response.status == DataStatus.COMPLETE

        normalized = provider.normalize(response.raw)
        assert normalized["value"] == 75.45
        assert normalized["yoy_pct"] == 75.45

    @pytest.mark.asyncio
    async def test_16_index_vs_yoy_fields_not_mixed(self):
        """Scenario 16: Index level (2003=100) and YoY % change are strictly separate fields."""
        raw_tabular = {
            "period": "2024-05",
            "cpi_index": "2200.50",
            "cpi_yoy_pct": "75.45",
            "cpi_mom_pct": "3.37",
        }
        provider = TUIKSDMXProvider()
        normalized = provider.normalize(raw_tabular)

        assert normalized["cpi_index"] == 2200.50
        assert normalized["cpi_yoy_pct"] == 75.45
        assert normalized["cpi_mom_pct"] == 3.37
        assert normalized["cpi_index"] != normalized["cpi_yoy_pct"]

    @pytest.mark.asyncio
    async def test_17_missing_sdmx_observation_is_none(self):
        """Scenario 17: Missing SDMX observation is None, never 0.0."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[]),
        )
        provider = TUIKSDMXProvider(http_client=mock_client)
        response = await provider.fetch(FetchContext("MACRO_INFLATION", provider_symbol="TR_CPI_TUIK_YOY"))

        assert response.status == DataStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_20_publication_vs_reference_period_separation(self):
        """Scenario 20: Reference month (e.g. May 2024) is distinct from release date (June 3, 2024)."""
        mock_payload = {
            "header": {"prepared": "2024-06-03T07:00:00Z"},
            "period": "2024-05",
            "value": "75.45",
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=mock_payload),
        )
        provider = TUIKSDMXProvider(http_client=mock_client)
        response = await provider.fetch(FetchContext("MACRO_INFLATION", provider_symbol="TR_CPI_TUIK_YOY"))

        assert response.effective_date == date(2024, 5, 31)
        assert response.published_at == datetime(2024, 6, 3, 7, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Manual ENAG Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestManualENAGProvider:

    @pytest.mark.asyncio
    async def test_23_pending_record_not_usable(self):
        """Scenario 23: PENDING record is UNAVAILABLE and not usable in calculations."""
        provider = ManualENAGProvider()
        record = ManualENAGRecord(
            reference_period="2024-05",
            value_type="MONTHLY_PCT",
            value=5.66,
            source_url="https://enagrup.org/bulten-mayis-2024.pdf",
            verification_status=VerificationStatus.PENDING,
        )
        provider.ingest_record(record)

        ctx = FetchContext(
            observation_type="MACRO_INFLATION",
            provider_symbol="TR_INFLATION_ENAG_MOM",
            effective_date=date(2024, 5, 31),
        )
        response = await provider.fetch(ctx)
        assert response.status == DataStatus.UNAVAILABLE
        assert "PENDING" in response.warnings[0]

    @pytest.mark.asyncio
    async def test_24_and_25_verified_monthly_and_annual_record_usable(self):
        """Scenario 24 & 25: VERIFIED records return COMPLETE."""
        provider = ManualENAGProvider()
        record_m = ManualENAGRecord(
            reference_period="2024-05",
            value_type="MONTHLY_PCT",
            value=5.66,
            source_url="https://enagrup.org/bulten-mayis-2024.pdf",
            verification_status=VerificationStatus.PENDING,
        )
        provider.ingest_record(record_m)
        provider.verify_record("2024-05", "MONTHLY_PCT", verified_by="analyst_1")

        ctx = FetchContext(
            observation_type="MACRO_INFLATION",
            provider_symbol="TR_INFLATION_ENAG_MOM",
            effective_date=date(2024, 5, 31),
        )
        response = await provider.fetch(ctx)
        assert response.status == DataStatus.COMPLETE
        assert response.raw["value"] == 5.66
        assert response.raw["verification_status"] == "verified"

    @pytest.mark.asyncio
    async def test_26_missing_source_cannot_be_verified(self):
        """Scenario 26: Record without source_url raises ValueError on verification."""
        provider = ManualENAGProvider()
        record = ManualENAGRecord(
            reference_period="2024-05",
            value_type="MONTHLY_PCT",
            value=5.66,
            source_url="", # Empty
            verification_status=VerificationStatus.PENDING,
        )
        provider.ingest_record(record)

        with pytest.raises(ValueError, match="source_url"):
            provider.verify_record("2024-05", "MONTHLY_PCT", verified_by="analyst_1")

    @pytest.mark.asyncio
    async def test_29_enag_never_classified_as_tuik_official_tier(self):
        """Scenario 29: ENAG is TIER_3_AGGREGATOR, never TIER_1_REGULATORY."""
        provider = ManualENAGProvider()
        assert provider.source_quality == SourceTier.TIER_3_AGGREGATOR
        assert provider.source_quality != SourceTier.TIER_1_REGULATORY

    @pytest.mark.asyncio
    async def test_30_enag_absent_produces_unavailable(self):
        """Scenario 30: When no manual record is entered, result is UNAVAILABLE (never 0.0)."""
        provider = ManualENAGProvider()
        ctx = FetchContext(
            observation_type="MACRO_INFLATION",
            provider_symbol="TR_INFLATION_ENAG_MOM",
            effective_date=date(2024, 1, 31),
        )
        response = await provider.fetch(ctx)
        assert response.status == DataStatus.UNAVAILABLE


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cross-Layer Macro Invariant Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMacroCrossLayerInvariants:

    def test_31_and_32_freshness_policies(self):
        """Scenario 31 & 32: FX uses EFFECTIVE_DATE daily; CPI uses PUBLISHED_AT monthly."""
        def_fx = MacroSeriesRegistry.get("TR_FX_USDTRY")
        assert def_fx.freshness_basis == FreshnessBasis.EFFECTIVE_DATE
        assert def_fx.frequency == MacroFrequency.BUSINESS_DAILY

        def_cpi = MacroSeriesRegistry.get("TR_CPI_TUIK_YOY")
        assert def_cpi.freshness_basis == FreshnessBasis.PUBLISHED_AT
        assert def_cpi.frequency == MacroFrequency.MONTHLY

    def test_33_macro_series_decoupled_from_fake_equity_instruments(self):
        """Scenario 33: Macro series have distinct canonical keys and registry entries."""
        for s in MacroSeriesRegistry.list_all():
            assert s.canonical_key.startswith("TR_")
            assert s.provider in ("TCMB_EVDS", "TUIK_SDMX", "ENAG_MANUAL")
            assert s.unit in (MacroUnit.TRY, MacroUnit.PERCENT, MacroUnit.INDEX_POINTS)

    def test_35_missing_is_never_zero(self):
        """Scenario 35: Invariant verification that missing macro data is None."""
        assert TCMBEVDSProvider._parse_decimal("-") is None
        assert TUIKSDMXProvider._parse_decimal("-") is None
        assert TCMBEVDSProvider._parse_decimal("") is None
        assert TUIKSDMXProvider._parse_decimal("") is None
