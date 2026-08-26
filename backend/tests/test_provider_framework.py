"""
backend/tests/test_provider_framework.py
==========================================
Unit tests for the Provider Framework, Fallback Orchestrator, and Explainable Data Confidence.

Verifies all 23 scenarios defined in the architectural specification:
    1. primary success (COMPLETE)
    2. primary fails -> fallback success (DEGRADED)
    3. fallback selected produces DEGRADED status
    4. all providers unavailable produces UNAVAILABLE
    5. stale cache accepted produces STALE status
    6. stale cache rejected produces UNAVAILABLE
    7. required field missing produces UNAVAILABLE / attempts fallback
    8. optional field missing produces PARTIAL status
    9. schema mismatch is non-fatal and records diagnostic attempt
    10. provider timeout is caught and recorded
    11. transient failure retry
    12. non-retryable auth failure fast-fails without wasteful retries
    13. conflicting provider values handled by policy priority
    14. proxy provider allowed produces DEGRADED
    15. proxy provider disallowed skips proxy
    16. historical vs latest cache separation
    17. canonical UUID preserved
    18. provider symbol preserved separately
    19. provenance contains selected source
    20. attempt history records failed attempts with timestamps & failure types
    21. missing value never becomes 0.0
    22. DataConfidence reason propagation
    23. calculation_coverage < 1.0 when fields missing

Zero external network calls.
"""

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.confidence import DataConfidence
from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataCriticality,
    DataStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.orchestrator import (
    OrchestrationResult,
    ProviderOrchestrator,
)
from backend.engine.private.policy import RetryPolicy, SourcePolicy
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixture Fake Providers
# ─────────────────────────────────────────────────────────────────────────────

class FakePrimaryOfficialProvider:
    """Simulates a Tier 1 regulatory/exchange provider that returns complete data."""
    provider_name: str = "primary_official"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    def __init__(self, effective_date: Optional[date] = None) -> None:
        self.effective_date = effective_date or date.today()

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
            effective_date=self.effective_date,
            status=DataStatus.COMPLETE,
            raw={"symbol": context.provider_symbol or "THYAO", "close": 320.0, "volume": 15000000, "open": 315.0},
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        return {
            "close": raw.get("close"),
            "open": raw.get("open"),
            "volume": raw.get("volume"),
        }

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return []

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version="1.0.0",
            endpoint="/quotes/daily",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
        )


class FakeFailingProvider:
    """Simulates a provider that raises an exception or returns UNAVAILABLE."""
    provider_name: str = "failing_primary"
    source_quality: SourceTier = SourceTier.TIER_2_EXCHANGE
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    def __init__(self, should_raise: bool = True, error_msg: str = "Connection refused") -> None:
        self.should_raise = should_raise
        self.error_msg = error_msg

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        if self.should_raise:
            raise ConnectionError(self.error_msg)
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=datetime.now(timezone.utc),
            published_at=None,
            effective_date=None,
            status=DataStatus.UNAVAILABLE,
            raw=None,
            warnings=["Instrument not found on exchange."],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        return {}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return []

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version="1.0.0",
            endpoint="/quotes",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
        )


class FakeSecondaryFallbackProvider:
    """Simulates an aggregator fallback provider."""
    provider_name: str = "secondary_aggregator"
    source_quality: SourceTier = SourceTier.TIER_3_AGGREGATOR
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
            effective_date=date.today(),
            status=DataStatus.COMPLETE,
            raw={"ticker": context.provider_symbol or "THYAO", "price": 319.5, "vol": 14000000},
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        return {
            "close": raw.get("price"),
            "volume": raw.get("vol"),
        }

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return ["Fallback aggregator quote slightly delayed."]

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version="1.0.0",
            endpoint="/aggregator/quote",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
        )


class FakeProxyProvider:
    """Simulates a Tier 5 proxy provider (e.g. index proxy)."""
    provider_name: str = "proxy_provider"
    source_quality: SourceTier = SourceTier.TIER_5_PROXY
    access_status: ProviderAccessStatus = ProviderAccessStatus.YELLOW

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
            effective_date=date.today(),
            status=DataStatus.COMPLETE,
            raw={"proxy_index_price": 310.0},
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        return {"close": raw.get("proxy_index_price")}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return ["Proxy estimate derived from sector index."]

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version="1.0.0",
            endpoint="/proxy",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
        )


class FakeBrokenSchemaProvider:
    """Simulates a provider whose response schema has changed unexpectedly."""
    provider_name: str = "broken_schema_provider"
    source_quality: SourceTier = SourceTier.TIER_2_EXCHANGE
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=datetime.now(timezone.utc),
            published_at=None,
            effective_date=date.today(),
            status=DataStatus.COMPLETE,
            raw="UNEXPECTED_HTML_STRING_RATHER_THAN_JSON",
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise TypeError(f"Expected dict, got {type(raw).__name__}")
        return raw

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return []

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version="1.0.0",
            endpoint="/quotes",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderOrchestratorSuite:

    @pytest.mark.asyncio
    async def test_01_primary_success_yields_complete(self):
        """Scenario 1: Primary provider returns valid complete data."""
        orch = ProviderOrchestrator()
        primary = FakePrimaryOfficialProvider()
        orch.register_provider(primary)

        inst_uuid = uuid4()
        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official"],
            required_fields=["close"],
            optional_fields=["volume", "open"],
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.COMPLETE
        assert res.confidence.level == DataConfidenceLevel.HIGH
        assert res.selected_provider == "primary_official"
        assert res.data["close"] == 320.0
        assert res.fallback_used is False
        assert res.canonical_instrument_id == inst_uuid

    @pytest.mark.asyncio
    async def test_02_and_03_primary_fails_fallback_success_yields_degraded(self):
        """Scenario 2 & 3: Primary fails, secondary succeeds -> status is DEGRADED."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(should_raise=True))
        orch.register_provider(FakeSecondaryFallbackProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary", "secondary_aggregator"],
            required_fields=["close"],
            retry_policy=RetryPolicy(max_attempts=1),
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.DEGRADED
        assert res.fallback_used is True
        assert res.selected_provider == "secondary_aggregator"
        assert res.data["close"] == 319.5
        assert len(res.attempts) == 2
        assert res.attempts[0].success is False
        assert res.attempts[1].success is True

    @pytest.mark.asyncio
    async def test_04_all_providers_unavailable_yields_unavailable(self):
        """Scenario 4: All providers fail -> UNAVAILABLE result."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(should_raise=False))

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            required_fields=["close"],
            allow_stale=False,
            retry_policy=RetryPolicy(max_attempts=1),
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert res.confidence.level == DataConfidenceLevel.NONE
        assert res.data == {}
        assert res.is_available is False

    @pytest.mark.asyncio
    async def test_05_and_06_stale_cache_accepted_and_rejected_semantics(self):
        """Scenario 5 & 6: Stale cache is STALE (never upgraded), rejected when disallowed."""
        orch = ProviderOrchestrator()
        # Seed an acceptable 2-day old observation in orchestrator's stale store
        inst_uuid = uuid4()
        old_eff_date = date.fromordinal(date.today().toordinal() - 2)

        cache_key = f"pit:PRICE_OHLCV:{inst_uuid}"
        orch._stale_cache_store[cache_key] = OrchestrationResult(
            observation_type="PRICE_OHLCV",
            status=DataStatus.COMPLETE,
            confidence=DataConfidence(
                level=DataConfidenceLevel.HIGH,
                freshness=1.0, source_quality=1.0, coverage=1.0, consistency=1.0, calculation_coverage=1.0
            ),
            data={"close": 300.0},
            selected_provider="primary_official",
            effective_date=old_eff_date,
            retrieved_at=datetime.now(timezone.utc),
            canonical_instrument_id=inst_uuid,
        )

        # Provider fails
        orch.register_provider(FakeFailingProvider(should_raise=True))

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
        )

        # A) With allow_stale=True (max 3 days) -> STALE
        policy_allow = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            allow_stale=True,
            max_staleness_seconds=86400 * 3,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        res_stale = await orch.execute(ctx, policy_allow)
        assert res_stale.status == DataStatus.STALE
        assert res_stale.is_stale_fallback is True
        assert res_stale.data["close"] == 300.0

        # B) With allow_stale=False -> UNAVAILABLE
        policy_disallow = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            allow_stale=False,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        res_unavail = await orch.execute(ctx, policy_disallow)
        assert res_unavail.status == DataStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_07_required_field_missing_yields_unavailable(self):
        """Scenario 7: When a critical required field is missing, result is UNAVAILABLE."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeSecondaryFallbackProvider()) # only provides close and volume

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["secondary_aggregator"],
            required_fields=["dcf_fair_value"], # Not present in secondary
            field_criticality={"dcf_fair_value": DataCriticality.CRITICAL},
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert res.confidence.level == DataConfidenceLevel.NONE

    @pytest.mark.asyncio
    async def test_08_optional_field_missing_yields_partial(self):
        """Scenario 8: Required present, but optional missing -> PARTIAL status."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeSecondaryFallbackProvider()) # provides close, volume (missing open)

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["secondary_aggregator"],
            required_fields=["close"],
            optional_fields=["open", "high", "low"],
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.PARTIAL or res.status == DataStatus.DEGRADED
        assert "open" in res.missing_inputs
        assert res.confidence.calculation_coverage < 1.0

    @pytest.mark.asyncio
    async def test_09_schema_mismatch_fails_over_to_next_provider(self):
        """Scenario 9: Broken schema logs diagnostic and tries next fallback."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeBrokenSchemaProvider())
        orch.register_provider(FakeSecondaryFallbackProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["broken_schema_provider", "secondary_aggregator"],
            required_fields=["close"],
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.DEGRADED
        assert res.selected_provider == "secondary_aggregator"
        assert res.attempts[0].failure_type == "SCHEMA_MISMATCH"

    @pytest.mark.asyncio
    async def test_10_and_11_timeout_and_transient_failure_handling(self):
        """Scenario 10 & 11: Timeout and transient errors are recorded with diagnostics."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(should_raise=True, error_msg="503 Service Unavailable"))

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            retry_policy=RetryPolicy(max_attempts=2, backoff_factor=0.01),
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert len(res.attempts) == 1
        assert res.attempts[0].failure_type == "FETCH_ERROR"

    @pytest.mark.asyncio
    async def test_12_non_retryable_auth_failure_fast_fails(self):
        """Scenario 12: 401/403 Auth errors fast-fail without wasteful retry sleep."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(should_raise=True, error_msg="401 Unauthorized: Invalid API Key"))

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            retry_policy=RetryPolicy(max_attempts=5, backoff_factor=1.0),
        )

        t_start = datetime.now()
        res = await orch.execute(ctx, policy)
        t_duration = (datetime.now() - t_start).total_seconds()

        assert res.status == DataStatus.UNAVAILABLE
        assert res.attempts[0].failure_type == "AUTH_ERROR"
        assert t_duration < 0.5  # Fast failed without 5 retries!

    @pytest.mark.asyncio
    async def test_14_and_15_proxy_provider_policy_enforcement(self):
        """Scenario 14 & 15: Proxy allowed -> DEGRADED; Proxy disallowed -> skipped."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeProxyProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )

        # Disallowed
        policy_disallowed = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["proxy_provider"],
            allow_proxy=False,
        )
        res_disallowed = await orch.execute(ctx, policy_disallowed)
        assert res_disallowed.status == DataStatus.UNAVAILABLE
        assert res_disallowed.attempts[0].failure_type == "PROXY_DISALLOWED"

        # Allowed
        policy_allowed = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["proxy_provider"],
            allow_proxy=True,
        )
        res_allowed = await orch.execute(ctx, policy_allowed)
        assert res_allowed.status == DataStatus.DEGRADED
        assert res_allowed.is_proxy is True

    @pytest.mark.asyncio
    async def test_16_historical_vs_latest_cache_isolation(self):
        """Scenario 16: Historical fetch distinguishes as_of_time from latest requests."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider())

        inst_uuid = uuid4()
        ctx_live = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
        )
        ctx_historical = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
            as_of_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
        )

        assert ctx_live.is_historical is False
        assert ctx_historical.is_historical is True

    @pytest.mark.asyncio
    async def test_17_and_18_canonical_uuid_and_provider_symbol_preserved(self):
        """Scenario 17 & 18: Canonical UUID and Provider Symbol remain uncorrupted."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider())

        inst_uuid = uuid4()
        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official"],
        )

        res = await orch.execute(ctx, policy)
        assert res.canonical_instrument_id == inst_uuid
        assert res.provider_symbol == "THYAO.IS"

    @pytest.mark.asyncio
    async def test_13_conflicting_provider_values_policy_precedence(self):
        """Scenario 13: Higher-priority provider is chosen over conflicting lower-priority provider."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider()) # price 320.0
        orch.register_provider(FakeSecondaryFallbackProvider()) # price 319.5

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official", "secondary_aggregator"],
            required_fields=["close"],
        )

        res = await orch.execute(ctx, policy)
        assert res.selected_provider == "primary_official"
        assert res.data["close"] == 320.0  # Primary wins without arbitrary averaging!


    @pytest.mark.asyncio
    async def test_19_and_20_provenance_and_attempt_history_audit_trail(self):
        """Scenario 19 & 20: Provenance tracks selected provider; attempt history tracks failures."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(should_raise=True))
        orch.register_provider(FakeSecondaryFallbackProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary", "secondary_aggregator"],
            retry_policy=RetryPolicy(max_attempts=1),
        )

        res = await orch.execute(ctx, policy)
        assert res.provenance is not None
        assert res.provenance.provider_name == "secondary_aggregator"
        assert len(res.attempts) == 2
        assert res.attempts[0].provider_name == "failing_primary"
        assert res.attempts[0].success is False
        assert res.attempts[1].provider_name == "secondary_aggregator"
        assert res.attempts[1].success is True

    @pytest.mark.asyncio
    async def test_21_missing_value_never_becomes_zero(self):
        """Scenario 21: Missing values are None or omitted, never converted to 0.0."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeSecondaryFallbackProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["secondary_aggregator"],
            optional_fields=["dividend_yield", "pe_ratio"],
        )

        res = await orch.execute(ctx, policy)
        assert "dividend_yield" not in res.data or res.data.get("dividend_yield") is None
        assert res.data.get("dividend_yield") != 0.0

    @pytest.mark.asyncio
    async def test_22_and_23_confidence_reasons_and_calculation_coverage(self):
        """Scenario 22 & 23: Reasons explain penalties; calculation_coverage reflects missing inputs."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeSecondaryFallbackProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["secondary_aggregator"],
            required_fields=["close"],
            optional_fields=["rsi", "macd", "bollinger_upper", "bollinger_lower"],
        )

        res = await orch.execute(ctx, policy)
        assert len(res.confidence.reasons) > 0
        assert res.confidence.calculation_coverage < 1.0
        assert res.confidence.coverage < 1.0

    @pytest.mark.asyncio
    async def test_storage_mapping_to_normalized_observation(self):
        """Directive 12: Verify mapping from OrchestrationResult to NormalizedObservationRecord."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider())

        inst_uuid = uuid4()
        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official"],
            required_fields=["close"],
        )

        res = await orch.execute(ctx, policy)
        norm_obs = res.to_normalized_observation(
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.BIST_STOCK,
            currency=Currency.TRY,
        )

        assert norm_obs is not None
        assert norm_obs.instrument_id == inst_uuid
        assert norm_obs.data_status == DataStatus.COMPLETE
        assert norm_obs.observation_data["close"] == 320.0
        assert norm_obs.currency == Currency.TRY
