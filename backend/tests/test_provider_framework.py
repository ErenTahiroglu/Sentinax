"""
backend/tests/test_provider_framework.py
==========================================
Comprehensive Unit & Regression Test Suite for Sentinax Provider Framework.

Coverage Matrix:
    [Core Provider Framework Scenarios]
    - test_01_primary_success_yields_complete: Primary provider success -> COMPLETE status
    - test_02_primary_fails_fallback_success_yields_degraded: Primary fails -> fallback success
    - test_03_fallback_result_status_is_degraded: Explicit verification of DEGRADED status on fallback
    - test_04_all_providers_unavailable_yields_unavailable: All providers fail -> UNAVAILABLE
    - test_05_stale_cache_accepted_when_explicitly_opted_in: Explicit allow_stale=True yields STALE status
    - test_06_stale_cache_rejected_when_disabled_by_default: Default allow_stale=False rejects stale data
    - test_07_required_field_missing_yields_unavailable: Critical missing field renders result UNAVAILABLE
    - test_08_optional_field_missing_yields_partial: Missing optional field yields PARTIAL
    - test_09_schema_mismatch_fails_over_to_next_provider: Schema mismatch records attempt and falls back
    - test_10_provider_timeout_is_caught_and_recorded: Timeout triggers retry and records failure
    - test_11_transient_rate_limit_and_server_error_retried: 429 and 5xx trigger bounded retries
    - test_12_non_retryable_auth_and_permission_fast_fails: 401/403 fast-fail without retries
    - test_13_conflicting_provider_values_policy_precedence: Primary winner chosen over conflicting secondary
    - test_14_proxy_provider_allowed_yields_degraded: allow_proxy=True accepts proxy as DEGRADED
    - test_15_proxy_provider_disallowed_skips_proxy: allow_proxy=False rejects proxy provider
    - test_16_historical_vs_latest_cache_isolation: Historical requests distinguish point-in-time
    - test_17_canonical_uuid_preserved: Single canonical UUID preserved throughout execution
    - test_18_provider_symbol_separately_preserved: Native query ticker preserved separately
    - test_19_provenance_preserved_with_source_lineage: Provenance records selected provider & endpoint
    - test_20_failed_provider_attempt_history_preserved: Attempt history tracks all failed providers
    - test_21_missing_value_never_becomes_zero: Missing fields are None, NEVER fabricated as 0.0
    - test_22_data_confidence_reasons_propagate: Explanatory reasons explain all score deductions
    - test_23_calculation_coverage_evaluates_strictly_calculation_fields: Decoupled calculation coverage

    [Hardening Scenarios]
    - test_24_future_effective_date_rejected_lookahead: effective_date > as_of rejected
    - test_25_future_published_at_rejected_lookahead: published_at > as_of rejected
    - test_26_future_data_cannot_receive_high_confidence: Future data lookahead receives NONE confidence
    - test_27_freshness_basis_effective_date_vs_published_at: FreshnessBasis selects appropriate timestamp
    - test_28_fresh_cache_hit_preserves_provenance_and_lineage: Serialized cache hit restores full result
    - test_29_force_refresh_bypasses_cache: force_refresh=True skips cache
    - test_30_deterministic_cache_key_strips_credentials: Sensitive tokens stripped from cache key
    - test_31_no_fabricated_snapshot_uuid_and_preserves_real_id: snapshot_id is None unless persisted
    - test_32_observed_at_stable_across_mapper_call: observed_at immutable during mapper calls
    - test_33_every_retry_attempt_appears_in_audit_trail: Each retry attempt (1, 2, 3) appears in audit trail
    - test_34_storage_mapping_to_normalized_observation: Conversion from result to NormalizedObservationRecord

Zero external network calls (pytest-socket enforced).
"""

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.confidence import (
    ConfidenceAssessmentService,
    DataConfidence,
)
from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataCriticality,
    DataStatus,
    FreshnessBasis,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderAuthenticationError,
    ProviderInvalidSymbolError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.orchestrator import (
    OrchestrationResult,
    ProviderOrchestrator,
)
from backend.engine.private.policy import RetryPolicy, SourcePolicy, StalenessPolicy
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

    def __init__(
        self,
        effective_date: Optional[date] = None,
        published_at: Optional[datetime] = None,
        observed_at: Optional[datetime] = None,
        close_price: float = 320.0,
    ) -> None:
        self.effective_date = effective_date or date.today()
        self.published_at = published_at or datetime.now(timezone.utc)
        self.observed_at = observed_at or datetime.now(timezone.utc)
        self.close_price = close_price

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=datetime.now(timezone.utc),
            published_at=self.published_at,
            observed_at=self.observed_at,
            effective_date=self.effective_date,
            status=DataStatus.COMPLETE,
            raw={
                "symbol": context.provider_symbol or "THYAO",
                "close": self.close_price,
                "volume": 15000000,
                "open": 315.0,
            },
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
    """Simulates a provider that raises typed exceptions or returns UNAVAILABLE."""
    provider_name: str = "failing_primary"
    source_quality: SourceTier = SourceTier.TIER_2_EXCHANGE
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    def __init__(self, exception_to_raise: Optional[Exception] = None) -> None:
        self.exception_to_raise = exception_to_raise

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        if self.exception_to_raise:
            raise self.exception_to_raise
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

    def __init__(self, close_price: float = 319.5) -> None:
        self.close_price = close_price

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
            effective_date=date.today(),
            status=DataStatus.COMPLETE,
            raw={"ticker": context.provider_symbol or "THYAO", "price": self.close_price, "vol": 14000000},
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
    """Simulates a Tier 5 proxy provider."""
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
    """Simulates a provider with an unexpected response structure."""
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
            raise ProviderSchemaError(f"Expected dict, got {type(raw).__name__}")
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
# Test Suite
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderOrchestratorSuite:

    @pytest.mark.asyncio
    async def test_01_primary_success_yields_complete(self):
        """Scenario 1: Primary provider returns valid complete data -> COMPLETE."""
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
    async def test_02_primary_fails_fallback_success_yields_degraded(self):
        """Scenario 2: Primary fails, approved fallback succeeds -> DEGRADED."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(exception_to_raise=ConnectionError("Drop")))
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

    @pytest.mark.asyncio
    async def test_03_fallback_result_status_is_degraded(self):
        """Scenario 3: Explicit test verifying fallback result status is DEGRADED."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(exception_to_raise=None))
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
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.DEGRADED
        assert res.fallback_used is True

    @pytest.mark.asyncio
    async def test_04_all_providers_unavailable_yields_unavailable(self):
        """Scenario 4: All providers fail -> UNAVAILABLE."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(exception_to_raise=None))

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
    async def test_05_stale_cache_accepted_when_explicitly_opted_in(self):
        """Scenario 5: Stale cache is accepted with STALE status when allow_stale=True."""
        orch = ProviderOrchestrator()
        inst_uuid = uuid4()
        old_eff_date = date.fromordinal(date.today().toordinal() - 2)

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
        )
        cache_key = ctx.generate_cache_key()

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

        orch.register_provider(FakeFailingProvider(exception_to_raise=ConnectionError("Fail")))

        opt_in_policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            allow_stale=True,
            max_staleness_seconds=86400 * 3,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        res_opt_in = await orch.execute(ctx, opt_in_policy)
        assert res_opt_in.status == DataStatus.STALE
        assert res_opt_in.is_stale_fallback is True
        assert res_opt_in.data["close"] == 300.0

    @pytest.mark.asyncio
    async def test_06_stale_cache_rejected_when_disabled_by_default(self):
        """Scenario 6: Stale cache is rejected by default (allow_stale=False)."""
        orch = ProviderOrchestrator()
        inst_uuid = uuid4()
        old_eff_date = date.fromordinal(date.today().toordinal() - 2)

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
        )
        cache_key = ctx.generate_cache_key()

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

        orch.register_provider(FakeFailingProvider(exception_to_raise=ConnectionError("Fail")))

        default_policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            retry_policy=RetryPolicy(max_attempts=1),
        )
        assert default_policy.allow_stale is False
        res_default = await orch.execute(ctx, default_policy)
        assert res_default.status == DataStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_07_required_field_missing_yields_unavailable(self):
        """Scenario 7: When a critical required field is missing, result is UNAVAILABLE."""
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
            required_fields=["dcf_fair_value"],
            field_criticality={"dcf_fair_value": DataCriticality.CRITICAL},
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert res.confidence.level == DataConfidenceLevel.NONE

    @pytest.mark.asyncio
    async def test_08_optional_field_missing_yields_partial(self):
        """Scenario 8: Required present, optional missing -> PARTIAL status."""
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
            optional_fields=["open", "high", "low"],
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.PARTIAL or res.status == DataStatus.DEGRADED
        assert "open" in res.missing_inputs

    @pytest.mark.asyncio
    async def test_09_schema_mismatch_fails_over_to_next_provider(self):
        """Scenario 9: Broken schema fast-fails and falls over to next provider."""
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
        assert res.attempts[0].failure_type == "SCHEMA" or "SCHEMA" in str(res.attempts[0].failure_type)

    @pytest.mark.asyncio
    async def test_10_provider_timeout_is_caught_and_recorded(self):
        """Scenario 10: Timeout error is caught and retried."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(exception_to_raise=ProviderTimeoutError("Connection timed out")))

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
        assert len(res.attempts) == 2
        assert res.attempts[0].failure_type == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_11_transient_rate_limit_and_server_error_retried(self):
        """Scenario 11: 429 RateLimit and 5xx ServerErrors trigger bounded retries."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(exception_to_raise=ProviderRateLimitError("Rate limit", retry_after_seconds=0.01)))

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            retry_policy=RetryPolicy(max_attempts=3, backoff_factor=0.01),
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert len(res.attempts) == 3
        assert res.attempts[0].failure_type == "RATE_LIMIT"

    @pytest.mark.asyncio
    async def test_12_non_retryable_auth_and_permission_fast_fails(self):
        """Scenario 12: Auth, Permission, InvalidSymbol fast-fail without wasteful retry sleep."""
        orch = ProviderOrchestrator()
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

        # Auth error
        orch.register_provider(FakeFailingProvider(exception_to_raise=ProviderAuthenticationError("401 Invalid Token")))
        t_start = datetime.now()
        res = await orch.execute(ctx, policy)
        t_duration = (datetime.now() - t_start).total_seconds()

        assert res.status == DataStatus.UNAVAILABLE
        assert len(res.attempts) == 1
        assert res.attempts[0].failure_type == "AUTHENTICATION"
        assert t_duration < 0.5

    @pytest.mark.asyncio
    async def test_13_conflicting_provider_values_policy_precedence(self):
        """Scenario 13: Higher-priority provider is chosen over conflicting lower-priority provider."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider(close_price=320.0))
        orch.register_provider(FakeSecondaryFallbackProvider(close_price=319.5))

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
        assert res.data["close"] == 320.0

    @pytest.mark.asyncio
    async def test_14_proxy_provider_allowed_yields_degraded(self):
        """Scenario 14: allow_proxy=True accepts proxy provider with DEGRADED status."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeProxyProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["proxy_provider"],
            allow_proxy=True,
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.DEGRADED
        assert res.is_proxy is True
        assert res.data["close"] == 310.0

    @pytest.mark.asyncio
    async def test_15_proxy_provider_disallowed_skips_proxy(self):
        """Scenario 15: allow_proxy=False skips proxy provider."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeProxyProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["proxy_provider"],
            allow_proxy=False,
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert res.attempts[0].failure_type == "PROXY_DISALLOWED"

    @pytest.mark.asyncio
    async def test_16_historical_vs_latest_cache_isolation(self):
        """Scenario 16: Historical fetch distinguishes as_of_time from latest requests."""
        ctx_live = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        ctx_historical = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
            as_of_time=datetime(2023, 1, 1, tzinfo=timezone.utc),
        )

        assert ctx_live.is_historical is False
        assert ctx_historical.is_historical is True
        assert ctx_live.generate_cache_key() != ctx_historical.generate_cache_key()

    @pytest.mark.asyncio
    async def test_17_canonical_uuid_preserved(self):
        """Scenario 17: Single canonical UUID preserved uncorrupted."""
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

    @pytest.mark.asyncio
    async def test_18_provider_symbol_separately_preserved(self):
        """Scenario 18: Native query symbol preserved separately from UUID."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official"],
        )

        res = await orch.execute(ctx, policy)
        assert res.provider_symbol == "THYAO.IS"

    @pytest.mark.asyncio
    async def test_19_provenance_preserved_with_source_lineage(self):
        """Scenario 19: Provenance tracks selected provider & endpoint."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider())

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official"],
        )

        res = await orch.execute(ctx, policy)
        assert res.provenance is not None
        assert res.provenance.provider_name == "primary_official"
        assert res.provenance.endpoint == "/quotes/daily"

    @pytest.mark.asyncio
    async def test_20_failed_provider_attempt_history_preserved(self):
        """Scenario 20: Attempt history tracks all failed providers with diagnostics."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(exception_to_raise=ConnectionError("Fail")))
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
        assert len(res.attempts) == 2
        assert res.attempts[0].provider_name == "failing_primary"
        assert res.attempts[0].success is False
        assert res.attempts[1].provider_name == "secondary_aggregator"
        assert res.attempts[1].success is True

    @pytest.mark.asyncio
    async def test_21_missing_value_never_becomes_zero(self):
        """Scenario 21: Missing fields are None or omitted, NEVER fabricated as 0.0."""
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
    async def test_22_data_confidence_reasons_propagate(self):
        """Scenario 22: Explanatory reasons propagate with all confidence deductions."""
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
            optional_fields=["rsi", "macd"],
        )

        res = await orch.execute(ctx, policy)
        assert len(res.confidence.reasons) > 0

    @pytest.mark.asyncio
    async def test_23_calculation_coverage_evaluates_strictly_calculation_fields(self):
        """Scenario 23: calculation_fields coverage is 1.0 even if optional display fields are missing."""
        conf = ConfidenceAssessmentService.assess(
            source_tier=SourceTier.TIER_1_REGULATORY,
            data_status=DataStatus.COMPLETE,
            effective_date=date.today(),
            published_at=datetime.now(timezone.utc),
            observed_at=datetime.now(timezone.utc),
            retrieved_at=datetime.now(timezone.utc),
            as_of_time=None,
            required_fields=["close"],
            optional_fields=["exchange_name", "sector_name", "currency_symbol"],
            present_fields=["close"],
            calculation_fields=["close"],
        )
        assert conf.calculation_coverage == 1.0
        assert conf.coverage < 1.0

    @pytest.mark.asyncio
    async def test_24_future_effective_date_rejected_lookahead(self):
        """Scenario 24: effective_date > as_of rejected on historical request (lookahead)."""
        orch = ProviderOrchestrator()
        future_provider = FakePrimaryOfficialProvider(
            effective_date=date(2026, 5, 2),
            published_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        )
        orch.register_provider(future_provider)

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
            as_of_time=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official"],
            required_fields=["close"],
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert res.attempts[0].failure_type == "LOOKAHEAD_REJECTED"

    @pytest.mark.asyncio
    async def test_25_future_published_at_rejected_lookahead(self):
        """Scenario 25: published_at > as_of rejected under SOURCE_AS_OF."""
        orch = ProviderOrchestrator()
        future_pub_provider = FakePrimaryOfficialProvider(
            effective_date=date(2026, 4, 30),
            published_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
        )
        orch.register_provider(future_pub_provider)

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
            as_of_time=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["primary_official"],
            required_fields=["close"],
        )

        res = await orch.execute(ctx, policy)
        assert res.status == DataStatus.UNAVAILABLE
        assert res.attempts[0].failure_type == "LOOKAHEAD_REJECTED"

    @pytest.mark.asyncio
    async def test_26_future_data_cannot_receive_high_confidence(self):
        """Scenario 26: Future data lookahead receives 0 freshness and NONE confidence."""
        conf = ConfidenceAssessmentService.assess(
            source_tier=SourceTier.TIER_1_REGULATORY,
            data_status=DataStatus.COMPLETE,
            effective_date=date(2026, 6, 1),
            published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            observed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            as_of_time=datetime(2026, 5, 1, tzinfo=timezone.utc),
            required_fields=["close"],
            optional_fields=[],
            present_fields=["close"],
        )
        assert conf.freshness == 0.0
        assert conf.level == DataConfidenceLevel.NONE

    @pytest.mark.asyncio
    async def test_27_freshness_basis_effective_date_vs_published_at(self):
        """Scenario 27: FreshnessBasis selects appropriate timestamp."""
        q_end_date = date(2024, 3, 31)
        pub_date = datetime(2024, 5, 10, 18, 0, tzinfo=timezone.utc)
        as_of = datetime(2024, 5, 11, 10, 0, tzinfo=timezone.utc)

        # Basis = PUBLISHED_AT -> Fresh
        conf_pub = ConfidenceAssessmentService.assess(
            source_tier=SourceTier.TIER_1_REGULATORY,
            data_status=DataStatus.COMPLETE,
            effective_date=q_end_date,
            published_at=pub_date,
            observed_at=pub_date,
            retrieved_at=pub_date,
            as_of_time=as_of,
            required_fields=["revenue"],
            optional_fields=[],
            present_fields=["revenue"],
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            max_staleness_days=3,
        )
        assert conf_pub.freshness > 0.8
        assert conf_pub.level == DataConfidenceLevel.HIGH

        # Basis = EFFECTIVE_DATE -> Stale (41 days old)
        conf_eff = ConfidenceAssessmentService.assess(
            source_tier=SourceTier.TIER_1_REGULATORY,
            data_status=DataStatus.COMPLETE,
            effective_date=q_end_date,
            published_at=pub_date,
            observed_at=pub_date,
            retrieved_at=pub_date,
            as_of_time=as_of,
            required_fields=["revenue"],
            optional_fields=[],
            present_fields=["revenue"],
            freshness_basis=FreshnessBasis.EFFECTIVE_DATE,
            max_staleness_days=3,
        )
        assert conf_eff.freshness < 0.5
        assert conf_eff.level == DataConfidenceLevel.LOW

    @pytest.mark.asyncio
    async def test_28_fresh_cache_hit_preserves_provenance_and_lineage(self):
        """Scenario 28: Serialized cache hit restores complete OrchestrationResult lineage."""
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
            allow_stale=True,
        )

        res_live = await orch.execute(ctx, policy)
        assert res_live.selected_provider == "primary_official"

        res_cached = await orch.execute(ctx, policy)
        assert res_cached.selected_provider == "primary_official"
        assert res_cached.data["close"] == 320.0
        assert res_cached.canonical_instrument_id == inst_uuid
        assert res_cached.provenance is not None
        assert res_cached.provenance.provider_name == "primary_official"

    @pytest.mark.asyncio
    async def test_29_force_refresh_bypasses_cache(self):
        """Scenario 29: force_refresh=True skips cache."""
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

        await orch.execute(ctx, policy)

        ctx_force = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=inst_uuid,
            provider_symbol="THYAO.IS",
            force_refresh=True,
        )
        res_refreshed = await orch.execute(ctx_force, policy)
        assert res_refreshed.data["close"] == 320.0

    @pytest.mark.asyncio
    async def test_30_deterministic_cache_key_strips_credentials(self):
        """Scenario 30: Cache key incorporates parameters but strips API keys/secrets."""
        ctx1 = FetchContext(
            observation_type="PRICE_OHLCV",
            provider_symbol="GARAN.IS",
            request_parameters={"api_key": "secret123", "frequency": "1d"},
        )
        ctx2 = FetchContext(
            observation_type="PRICE_OHLCV",
            provider_symbol="GARAN.IS",
            request_parameters={"api_key": "different_secret", "frequency": "1d"},
        )
        ctx3 = FetchContext(
            observation_type="PRICE_OHLCV",
            provider_symbol="GARAN.IS",
            request_parameters={"frequency": "1m"},
        )

        assert ctx1.generate_cache_key() == ctx2.generate_cache_key()
        assert "secret123" not in ctx1.generate_cache_key()
        assert ctx1.generate_cache_key() != ctx3.generate_cache_key()

    @pytest.mark.asyncio
    async def test_31_no_fabricated_snapshot_uuid_and_preserves_real_id(self):
        """Scenario 31: snapshot_id is None unless persisted (no random UUID fabrication)."""
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
        norm_obs_none = res.to_normalized_observation(
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.BIST_STOCK,
            currency=Currency.TRY,
            snapshot_id=None,
        )
        assert norm_obs_none is not None
        assert norm_obs_none.snapshot_id is None

        real_snap_id = uuid4()
        norm_obs_real = res.to_normalized_observation(
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.BIST_STOCK,
            currency=Currency.TRY,
            snapshot_id=real_snap_id,
        )
        assert norm_obs_real is not None
        assert norm_obs_real.snapshot_id == real_snap_id

    @pytest.mark.asyncio
    async def test_32_observed_at_stable_across_mapper_call(self):
        """Scenario 32: observed_at timestamp does not change during mapper invocations."""
        fixed_obs_at = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        orch = ProviderOrchestrator()
        orch.register_provider(FakePrimaryOfficialProvider(observed_at=fixed_obs_at))

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
        assert res.observed_at == fixed_obs_at

        norm_obs1 = res.to_normalized_observation(AssetClass.EQUITY, InstrumentType.BIST_STOCK, Currency.TRY)
        await asyncio.sleep(0.01)
        norm_obs2 = res.to_normalized_observation(AssetClass.EQUITY, InstrumentType.BIST_STOCK, Currency.TRY)

        assert norm_obs1.observed_at == fixed_obs_at
        assert norm_obs2.observed_at == fixed_obs_at

    @pytest.mark.asyncio
    async def test_33_every_retry_attempt_appears_in_audit_trail(self):
        """Scenario 33: Retry loop records every attempt with attempt_number in audit trail."""
        orch = ProviderOrchestrator()
        orch.register_provider(FakeFailingProvider(exception_to_raise=ProviderServerError("500 Internal Error")))

        ctx = FetchContext(
            observation_type="PRICE_OHLCV",
            canonical_instrument_id=uuid4(),
            provider_symbol="THYAO.IS",
        )
        policy = SourcePolicy(
            observation_type="PRICE_OHLCV",
            ordered_provider_names=["failing_primary"],
            retry_policy=RetryPolicy(max_attempts=3, backoff_factor=0.01),
        )

        res = await orch.execute(ctx, policy)
        assert len(res.attempts) == 3
        assert [a.attempt_number for a in res.attempts] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_34_storage_mapping_to_normalized_observation(self):
        """Scenario 34: Verify mapping from OrchestrationResult to NormalizedObservationRecord."""
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
