"""
backend/engine/private/orchestrator.py
========================================
Fallback Orchestrator & Graceful Degradation Engine for Private Engine.

Core Principles:
    - Sits strictly above individual data providers.
    - Resolves data requests by evaluating declarative SourcePolicy chains.
    - Enforces full graceful degradation across all failure modes.
    - Tracks complete diagnostic attempt history (no silent failures).
    - Ensures Missing Data ≠ Zero.
    - Prevents stale data from masquerading as COMPLETE.
    - Reuses infrastructure caching (Redis / in-memory fallback).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from backend.engine.private.confidence import (
    ConfidenceAssessmentService,
    DataConfidence,
)
from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataCriticality,
    DataStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.policy import RetryPolicy, SourcePolicy
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderAttempt,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.storage_models import (
    NormalizedObservationRecord,
    RawProviderSnapshotRecord,
)
from backend.infrastructure.redis_cache import cache_get, cache_set

logger = logging.getLogger(__name__)


@dataclass
class OrchestrationResult:
    """
    Standardized aggregate output of the ProviderOrchestrator.
    Contains the normalized data, explainable confidence, and diagnostic audit trail.
    """
    observation_type: str
    status: DataStatus
    confidence: DataConfidence
    data: Dict[str, Any] = field(default_factory=dict)
    selected_provider: Optional[str] = None
    fallback_used: bool = False
    is_stale_fallback: bool = False
    is_proxy: bool = False
    effective_date: Optional[date] = None
    published_at: Optional[datetime] = None
    observed_at: Optional[datetime] = None
    retrieved_at: Optional[datetime] = None
    canonical_instrument_id: Optional[UUID] = None
    provider_symbol: Optional[str] = None
    attempts: List[ProviderAttempt] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_inputs: List[str] = field(default_factory=list)
    provenance: Optional[ProviderProvenance] = None
    raw_payload: Any = None

    @property
    def is_available(self) -> bool:
        return self.status != DataStatus.UNAVAILABLE

    def to_normalized_observation(
        self,
        asset_class: AssetClass,
        instrument_type: InstrumentType,
        currency: Currency,
        snapshot_id: Optional[UUID] = None,
    ) -> Optional[NormalizedObservationRecord]:
        """
        Maps the orchestration result to a canonical storage record.
        Returns None if data is UNAVAILABLE or missing required identity/effective_date.
        """
        if not self.is_available or self.canonical_instrument_id is None or self.effective_date is None:
            return None

        return NormalizedObservationRecord(
            snapshot_id=snapshot_id or uuid4(),
            instrument_id=self.canonical_instrument_id,
            asset_class=asset_class,
            instrument_type=instrument_type,
            observation_type=self.observation_type,
            observation_data=self.data,
            data_status=self.status,
            confidence_level=self.confidence.level,
            source_tier=self.provenance.source_quality if self.provenance else SourceTier.TIER_4_DERIVED,
            currency=currency,
            effective_date=self.effective_date,
            published_at=self.published_at,
            observed_at=self.observed_at or datetime.now(timezone.utc),
            missing_inputs=self.missing_inputs,
            warnings=self.warnings,
            source_refs=[self.provenance.to_source_ref()] if hasattr(self.provenance, "to_source_ref") else [],
        )

    def to_raw_snapshot(
        self,
        endpoint: str = "/fetch",
        request_params: Optional[Dict[str, Any]] = None,
    ) -> Optional[RawProviderSnapshotRecord]:
        """
        Maps raw response payload to immutable RawProviderSnapshotRecord.
        """
        if self.raw_payload is None or self.selected_provider is None:
            return None

        return RawProviderSnapshotRecord.create(
            provider=self.selected_provider,
            endpoint=endpoint,
            request_params=request_params or {},
            raw_payload=self.raw_payload,
            retrieved_at=self.retrieved_at,
        )


class ProviderOrchestrator:
    """
    Decoupled orchestrator that executes fallback chains and produces explainable results.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, DataProviderContract] = {}
        self._stale_cache_store: Dict[str, OrchestrationResult] = {}

    def register_provider(self, provider: DataProviderContract) -> None:
        """Register a provider instance with the orchestrator."""
        self._providers[provider.provider_name] = provider

    def get_provider(self, provider_name: str) -> Optional[DataProviderContract]:
        return self._providers.get(provider_name)

    async def execute(
        self,
        context: FetchContext,
        policy: SourcePolicy,
    ) -> OrchestrationResult:
        """
        Executes data sourcing across the configured provider fallback chain.
        """
        attempts: List[ProviderAttempt] = []
        warnings: List[str] = []

        cache_key = f"pit:{context.observation_type}:{context.canonical_instrument_id or context.provider_symbol}"
        if context.is_historical:
            cache_key += f":asof_{context.as_of_time.isoformat() if context.as_of_time else ''}"

        # 1. Check live memory cache (if not forced refresh and not historical)
        if not context.force_refresh and not context.is_historical:
            cached_data = cache_get(cache_key)
            if cached_data and isinstance(cached_data, dict):
                # Valid fresh cached result
                pass

        # 2. Iterate through ordered provider fallback chain
        for idx, provider_name in enumerate(policy.ordered_provider_names):
            provider = self._providers.get(provider_name)
            is_fallback = idx > 0

            # Guard: Unregistered provider
            if not provider:
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        success=False,
                        failure_type="UNREGISTERED",
                        message=f"Provider '{provider_name}' is not registered in orchestrator.",
                    )
                )
                continue

            # Guard: Proxy restriction
            is_proxy = provider.source_quality == SourceTier.TIER_5_PROXY
            if is_proxy and not policy.allow_proxy:
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        success=False,
                        failure_type="PROXY_DISALLOWED",
                        message="Proxy provider disallowed by policy for this observation.",
                    )
                )
                continue

            # Guard: Minimum source tier
            if self._tier_rank(provider.source_quality) < self._tier_rank(policy.minimum_source_tier):
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        success=False,
                        failure_type="TIER_BELOW_MINIMUM",
                        message=f"Source tier {provider.source_quality.value} is below required {policy.minimum_source_tier.value}.",
                    )
                )
                continue

            # Guard: Provider access status (RED)
            if provider.access_status == ProviderAccessStatus.RED:
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        success=False,
                        failure_type="PROVIDER_RED",
                        message=f"Provider '{provider_name}' operational status is RED (down/blocked).",
                    )
                )
                continue

            # 3. Attempt Fetch with Retry Loop
            t_start = time.perf_counter()
            response = await self._fetch_with_retry(provider, context, policy.retry_policy, attempts)
            t_elapsed_ms = (time.perf_counter() - t_start) * 1000.0

            if response is None or not response.is_usable:
                continue

            # 4. Normalize & Validate
            try:
                normalized = provider.normalize(response.raw)
                val_warnings = provider.validate(normalized)
            except Exception as e:
                logger.error(f"Normalization error in provider '{provider_name}': {e}")
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        success=False,
                        failure_type="SCHEMA_MISMATCH",
                        message=f"Normalization failed: {str(e)}",
                        latency_ms=t_elapsed_ms,
                    )
                )
                continue

            # Check required & optional fields
            present_fields = list(normalized.keys())
            missing_req = [f for f in policy.required_fields if f not in present_fields or normalized[f] is None]
            missing_opt = [f for f in policy.optional_fields if f not in present_fields or normalized[f] is None]
            all_missing = missing_req + missing_opt

            # Check critical field missing
            has_critical_missing = any(
                policy.field_criticality.get(f) == DataCriticality.CRITICAL for f in missing_req
            )

            # If critical field is missing on non-final provider, we may attempt next fallback
            if has_critical_missing and idx < len(policy.ordered_provider_names) - 1:
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        success=False,
                        failure_type="CRITICAL_FIELD_MISSING",
                        message=f"Critical fields missing: {missing_req}. Attempting fallback.",
                        latency_ms=t_elapsed_ms,
                    )
                )
                continue

            # 5. Determine DataStatus
            if has_critical_missing:
                status = DataStatus.UNAVAILABLE
            elif is_proxy:
                status = DataStatus.DEGRADED
            elif is_fallback:
                status = DataStatus.DEGRADED
            elif missing_opt:
                status = DataStatus.PARTIAL
            else:
                status = DataStatus.COMPLETE

            all_warnings = warnings + response.warnings + val_warnings

            # 6. Assess Explainable Confidence
            confidence = ConfidenceAssessmentService.assess(
                source_tier=provider.source_quality,
                data_status=status,
                effective_date=response.effective_date,
                retrieved_at=response.retrieved_at,
                as_of_time=context.as_of_time,
                required_fields=policy.required_fields,
                optional_fields=policy.optional_fields,
                present_fields=present_fields,
                field_criticality=policy.field_criticality,
                warnings=all_warnings,
                is_fallback=is_fallback,
                is_proxy=is_proxy,
                max_staleness_days=policy.max_staleness_seconds // 86400,
            )

            # Record success attempt
            attempts.append(
                ProviderAttempt(
                    provider_name=provider_name,
                    success=True,
                    latency_ms=t_elapsed_ms,
                )
            )

            provenance = provider.provenance(response)
            now_utc = datetime.now(timezone.utc)

            result = OrchestrationResult(
                observation_type=context.observation_type,
                status=status,
                confidence=confidence,
                data=normalized,
                selected_provider=provider_name,
                fallback_used=is_fallback,
                is_proxy=is_proxy,
                effective_date=response.effective_date,
                published_at=response.published_at,
                observed_at=now_utc,
                retrieved_at=response.retrieved_at,
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
                attempts=attempts,
                warnings=all_warnings,
                missing_inputs=all_missing,
                provenance=provenance,
                raw_payload=response.raw,
            )

            # Save in stale cache store for future fallback
            if status != DataStatus.UNAVAILABLE:
                self._stale_cache_store[cache_key] = result
                if not context.is_historical:
                    cache_set(cache_key, normalized, ttl=policy.max_staleness_seconds)

            return result

        # 7. All providers in chain failed — Check acceptable stale cache
        if policy.allow_stale and cache_key in self._stale_cache_store:
            cached_res = self._stale_cache_store[cache_key]
            if policy.staleness_policy.is_acceptable(cached_res.effective_date, context.as_of_time):
                # Assess confidence with stale penalty
                stale_warnings = list(cached_res.warnings) + ["All live providers failed; using acceptable stale cached data."]
                stale_conf = ConfidenceAssessmentService.assess(
                    source_tier=cached_res.provenance.source_quality if cached_res.provenance else SourceTier.TIER_5_PROXY,
                    data_status=DataStatus.STALE,
                    effective_date=cached_res.effective_date,
                    retrieved_at=cached_res.retrieved_at,
                    as_of_time=context.as_of_time,
                    required_fields=policy.required_fields,
                    optional_fields=policy.optional_fields,
                    present_fields=list(cached_res.data.keys()),
                    field_criticality=policy.field_criticality,
                    warnings=stale_warnings,
                    is_fallback=True,
                    max_staleness_days=policy.max_staleness_seconds // 86400,
                )

                return OrchestrationResult(
                    observation_type=context.observation_type,
                    status=DataStatus.STALE,
                    confidence=stale_conf,
                    data=cached_res.data,
                    selected_provider=cached_res.selected_provider,
                    fallback_used=True,
                    is_stale_fallback=True,
                    effective_date=cached_res.effective_date,
                    published_at=cached_res.published_at,
                    observed_at=cached_res.observed_at,
                    retrieved_at=cached_res.retrieved_at,
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                    attempts=attempts,
                    warnings=stale_warnings,
                    missing_inputs=cached_res.missing_inputs,
                    provenance=cached_res.provenance,
                    raw_payload=cached_res.raw_payload,
                )

        # 8. Complete Failure — UNAVAILABLE
        reasons = [f"All {len(policy.ordered_provider_names)} providers failed."]
        for a in attempts:
            if not a.success:
                reasons.append(f"{a.provider_name}: {a.failure_type} ({a.message or 'No details'})")

        return OrchestrationResult(
            observation_type=context.observation_type,
            status=DataStatus.UNAVAILABLE,
            confidence=DataConfidence.unavailable(reasons=reasons),
            data={},
            selected_provider=None,
            fallback_used=False,
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
            attempts=attempts,
            warnings=["No usable data retrieved."],
            missing_inputs=policy.required_fields,
        )

    async def _fetch_with_retry(
        self,
        provider: DataProviderContract,
        context: FetchContext,
        retry_policy: RetryPolicy,
        attempts: List[ProviderAttempt],
    ) -> Optional[ProviderResponse]:
        """Executes fetch with bounded retries on transient errors."""
        max_attempts = retry_policy.max_attempts

        for attempt_no in range(1, max_attempts + 1):
            t_start = time.perf_counter()
            try:
                response = await provider.fetch(context)
                if response.status == DataStatus.UNAVAILABLE:
                    # Non-retryable missing data / 404
                    attempts.append(
                        ProviderAttempt(
                            provider_name=provider.provider_name,
                            success=False,
                            failure_type="UNAVAILABLE",
                            message=f"Provider returned UNAVAILABLE: {', '.join(response.warnings)}",
                            latency_ms=(time.perf_counter() - t_start) * 1000.0,
                        )
                    )
                    return None
                return response
            except asyncio.TimeoutError:
                is_last = attempt_no == max_attempts
                latency = (time.perf_counter() - t_start) * 1000.0
                if is_last:
                    attempts.append(
                        ProviderAttempt(
                            provider_name=provider.provider_name,
                            success=False,
                            failure_type="TIMEOUT",
                            message=f"Request timed out after {max_attempts} attempts.",
                            latency_ms=latency,
                        )
                    )
                    return None
                await asyncio.sleep(retry_policy.backoff_factor * attempt_no)
            except Exception as e:
                err_str = str(e)
                latency = (time.perf_counter() - t_start) * 1000.0
                
                # Check if error is non-retryable auth / permission
                if "401" in err_str or "403" in err_str or "auth" in err_str.lower():
                    attempts.append(
                        ProviderAttempt(
                            provider_name=provider.provider_name,
                            success=False,
                            failure_type="AUTH_ERROR",
                            message=f"Non-retryable credentials error: {err_str}",
                            latency_ms=latency,
                        )
                    )
                    return None

                is_last = attempt_no == max_attempts
                if is_last:
                    attempts.append(
                        ProviderAttempt(
                            provider_name=provider.provider_name,
                            success=False,
                            failure_type="FETCH_ERROR",
                            message=err_str,
                            latency_ms=latency,
                        )
                    )
                    return None
                await asyncio.sleep(retry_policy.backoff_factor * attempt_no)
        return None

    @staticmethod
    def _tier_rank(tier: SourceTier) -> int:
        """Ordinal rank of SourceTier (higher is better)."""
        ranks = {
            SourceTier.TIER_1_REGULATORY: 5,
            SourceTier.TIER_2_EXCHANGE: 4,
            SourceTier.TIER_3_AGGREGATOR: 3,
            SourceTier.TIER_4_DERIVED: 2,
            SourceTier.TIER_5_PROXY: 1,
        }
        return ranks.get(tier, 0)
