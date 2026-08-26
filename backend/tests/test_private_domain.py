"""
tests/test_private_domain.py
================================
Test suite for backend/engine/private/ — core domain contracts.

Verified:
    - Enum values and membership
    - DataResult factory method semantics (MISSING != ZERO)
    - PARTIAL analysis semantics
    - AnalysisResult aggregate status derivation
    - ProviderResponse serialization
    - DataProviderContract protocol conformance check

No external network calls. No mocking required — pure unit tests.
"""

import pytest
from datetime import date, datetime, timezone

from backend.engine.private.domain import (
    AssetClass,
    InstrumentType,
    PortfolioMode,
    DataStatus,
    DataConfidenceLevel,
    SourceTier,
    Horizon,
    Currency,
    TaxConfidenceClass,
)
from backend.engine.private.result import DataResult, AnalysisResult
from backend.engine.private.provider_contract import (
    DataProviderContract,
    ProviderResponse,
    ProviderProvenance,
)


# ─────────────────────────────────────────────────────────────────────────────
# Domain Enum Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDomainEnums:
    def test_asset_class_has_all_required_members(self):
        expected = {"EQUITY", "FUND", "COMMODITY", "FX", "FIXED_INCOME", "ETF"}
        assert expected.issubset({m.name for m in AssetClass})

    def test_crypto_is_explicitly_out_of_scope(self):
        """CRYPTO must be present in InstrumentType only to allow rejection at ingestion."""
        assert InstrumentType.CRYPTO in InstrumentType
        # It must NOT be in AssetClass — crypto has no asset class in this engine
        assert not any("CRYPTO" in m.name for m in AssetClass)

    def test_portfolio_mode_values(self):
        assert PortfolioMode.MY_PORTFOLIO.value == "my_portfolio"
        assert PortfolioMode.SANDBOX.value == "sandbox"

    def test_data_status_ordering_readable(self):
        """All five statuses must exist — they appear in API responses."""
        names = {s.name for s in DataStatus}
        assert {"COMPLETE", "PARTIAL", "DEGRADED", "STALE", "UNAVAILABLE"}.issubset(names)

    def test_tax_confidence_class_three_tiers(self):
        names = {t.name for t in TaxConfidenceClass}
        assert "DETERMINISTIC" in names
        assert "USER_INCOME_DEPENDENT" in names
        assert "PROFESSIONAL_VALIDATION_REQUIRED" in names

    def test_currency_includes_gold_and_silver(self):
        assert Currency.XAU in Currency
        assert Currency.XAG in Currency


# ─────────────────────────────────────────────────────────────────────────────
# DataResult — Missing Data Semantics (MISSING != ZERO)
# ─────────────────────────────────────────────────────────────────────────────

class TestDataResultMissingNotZero:
    def test_unavailable_value_is_none_not_zero(self):
        """Core invariant: missing data is None, never 0."""
        result = DataResult.unavailable(missing_inputs=["revenue"])
        assert result.value is None, "MISSING must be None, not zero!"
        assert result.status == DataStatus.UNAVAILABLE

    def test_unavailable_confidence_is_none(self):
        result = DataResult.unavailable(missing_inputs=["price"])
        assert result.confidence == DataConfidenceLevel.NONE

    def test_complete_factory_rejects_none_value(self):
        """complete() must not accept None — that's what unavailable() is for."""
        with pytest.raises(ValueError, match="non-None"):
            DataResult.complete(value=None, as_of=date.today())

    def test_partial_factory_rejects_empty_missing_inputs(self):
        """partial() without missing_inputs is logically complete — must raise."""
        with pytest.raises(ValueError, match="missing_input"):
            DataResult.partial(value=42.0, missing_inputs=[])

    def test_complete_result_is_available(self):
        r = DataResult.complete(value=15.3, as_of=date.today())
        assert r.is_available is True
        assert r.is_complete is True

    def test_unavailable_result_is_not_available(self):
        r = DataResult.unavailable(missing_inputs=["roe"])
        assert r.is_available is False

    def test_stale_result_is_available_with_low_confidence(self):
        r = DataResult.stale(value=100.0, as_of=date(2023, 1, 1))
        assert r.is_available is True
        assert r.confidence == DataConfidenceLevel.LOW
        assert r.status == DataStatus.STALE


# ─────────────────────────────────────────────────────────────────────────────
# DataResult — PARTIAL Analysis Semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestDataResultPartialSemantics:
    def test_partial_has_value_and_missing_inputs(self):
        """
        Scenario: ROE is available, valuation inputs missing.
        Quality analysis can run; valuation UNAVAILABLE → PARTIAL.
        """
        result = DataResult.partial(
            value=72.0,
            missing_inputs=["ev_ebitda", "fcf_yield"],
            warnings=["Valuation inputs absent; score computed from quality metrics only."],
        )
        assert result.value == 72.0
        assert result.status == DataStatus.PARTIAL
        assert "ev_ebitda" in result.missing_inputs
        assert result.is_available is True

    def test_partial_result_serialises_correctly(self):
        r = DataResult.partial(value=55.0, missing_inputs=["net_income"])
        d = r.to_dict()
        assert d["status"] == "partial"
        assert d["value"] == 55.0
        assert "net_income" in d["missing_inputs"]


# ─────────────────────────────────────────────────────────────────────────────
# AnalysisResult — Aggregate Status Derivation
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalysisResultAggregation:
    """
    Core scenario from spec:
        quality analysis: ROE available → COMPLETE quality score
        valuation: no inputs → UNAVAILABLE
        => aggregate status: PARTIAL (not a crash, not COMPLETE)
    """

    def test_all_complete_yields_complete(self):
        components = {
            "quality": DataResult.complete(value=80.0, as_of=date.today()),
            "liquidity": DataResult.complete(value=1.5, as_of=date.today()),
        }
        result = AnalysisResult.from_components(components, datetime.now(timezone.utc))
        assert result.status == DataStatus.COMPLETE

    def test_mixed_complete_and_unavailable_yields_partial(self):
        """The key acceptance criterion: partial is valid, not a crash."""
        components = {
            "quality": DataResult.complete(value=72.0, as_of=date.today()),
            "valuation": DataResult.unavailable(missing_inputs=["ev", "ebitda"]),
        }
        result = AnalysisResult.from_components(components, datetime.now(timezone.utc))
        assert result.status == DataStatus.PARTIAL

    def test_all_unavailable_yields_unavailable(self):
        components = {
            "quality": DataResult.unavailable(missing_inputs=["roe"]),
            "valuation": DataResult.unavailable(missing_inputs=["ev"]),
        }
        result = AnalysisResult.from_components(components, datetime.now(timezone.utc))
        assert result.status == DataStatus.UNAVAILABLE

    def test_stale_without_unavailable_yields_degraded(self):
        components = {
            "price": DataResult.stale(value=150.0, as_of=date(2023, 6, 1)),
            "volume": DataResult.complete(value=1_000_000, as_of=date.today()),
        }
        result = AnalysisResult.from_components(components, datetime.now(timezone.utc))
        assert result.status == DataStatus.DEGRADED

    def test_partial_components_accessible_individually(self):
        """Consumer must be able to access COMPLETE components even when aggregate is PARTIAL."""
        components = {
            "quality": DataResult.complete(value=80.0, as_of=date.today()),
            "valuation": DataResult.unavailable(missing_inputs=["dcf_input"]),
        }
        result = AnalysisResult.from_components(components, datetime.now(timezone.utc))
        assert result.components["quality"].is_available is True
        assert result.components["valuation"].is_available is False

    def test_to_dict_serialisation(self):
        components = {
            "quality": DataResult.complete(value=75.0, as_of=date.today()),
            "valuation": DataResult.unavailable(missing_inputs=["revenue"]),
        }
        result = AnalysisResult.from_components(components, datetime.now(timezone.utc))
        d = result.to_dict()
        assert d["status"] == "partial"
        assert "components" in d
        assert d["components"]["valuation"]["value"] is None


# ─────────────────────────────────────────────────────────────────────────────
# ProviderResponse & Contract
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderResponse:
    def test_provider_response_instantiation(self):
        now = datetime.now(timezone.utc)
        resp = ProviderResponse(
            provider_name="test_provider",
            source_quality=SourceTier.TIER_3_AGGREGATOR,
            retrieved_at=now,
            published_at=None,
            effective_date=date.today(),
            status=DataStatus.COMPLETE,
            raw={"close": 150.0},
            instrument_id="THYAO.IS",
        )
        assert resp.is_usable is True
        assert resp.provider_name == "test_provider"

    def test_unavailable_response_not_usable(self):
        now = datetime.now(timezone.utc)
        resp = ProviderResponse(
            provider_name="test_provider",
            source_quality=SourceTier.TIER_5_PROXY,
            retrieved_at=now,
            published_at=None,
            effective_date=None,
            status=DataStatus.UNAVAILABLE,
            raw=None,
            instrument_id="UNKNOWN",
            warnings=["Instrument not found in provider database."],
        )
        assert resp.is_usable is False

    def test_to_source_ref_format(self):
        now = datetime.now(timezone.utc)
        resp = ProviderResponse(
            provider_name="bist_feed",
            source_quality=SourceTier.TIER_2_EXCHANGE,
            retrieved_at=now,
            published_at=None,
            effective_date=date(2024, 3, 31),
            status=DataStatus.COMPLETE,
            raw={},
            instrument_id="GARAN.IS",
        )
        ref = resp.to_source_ref()
        assert "bist_feed" in ref
        assert "GARAN.IS" in ref
        assert "2024-03-31" in ref

    def test_data_provider_contract_is_protocol(self):
        """DataProviderContract must be a runtime-checkable Protocol."""
        assert hasattr(DataProviderContract, "__protocol_attrs__") or \
               hasattr(DataProviderContract, "_is_protocol")

    def test_non_conforming_object_fails_isinstance_check(self):
        class NotAProvider:
            pass
        assert not isinstance(NotAProvider(), DataProviderContract)
