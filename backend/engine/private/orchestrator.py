"""
backend/engine/private/orchestrator.py
========================================
Fallback Orchestrator & Graceful Degradation Engine for Private Engine.

Core Principles:
    - Sits strictly above individual data providers.
    - Resolves data requests by evaluating declarative SourcePolicy chains.
    - Enforces full graceful degradation across all failure modes.
    - Tracks complete diagnostic attempt history including retry counts (no silent failures).
    - Ensures Missing Data ≠ Zero.
    - Prevents stale data from masquerading as COMPLETE (stale is opt-in).
    - Enforces lookahead protection on historical requests (future data rejected).
    - Reuses infrastructure caching (Redis / in-memory fallback) with full provenance preservation.
    - Never fabricates random UUIDs for missing raw snapshot foreign keys.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

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
from backend.engine.private.exceptions import (
    NonRetryableProviderError,
    ProviderAuthenticationError,
    ProviderInvalidSymbolError,
    ProviderLookaheadError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    TransientProviderError,
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
    source_metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_available(self) -> bool:
        return self.status != DataStatus.UNAVAILABLE

    def to_dict(self) -> Dict[str, Any]:
        """Serializes full result preserving lineage and diagnostic history."""
        return {
            "observation_type": self.observation_type,
            "status": self.status.value,
            "confidence": self.confidence.to_dict(),
            "data": self.data,
            "selected_provider": self.selected_provider,
            "fallback_used": self.fallback_used,
            "is_stale_fallback": self.is_stale_fallback,
            "is_proxy": self.is_proxy,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "canonical_instrument_id": str(self.canonical_instrument_id) if self.canonical_instrument_id else None,
            "provider_symbol": self.provider_symbol,
            "attempts": [a.to_dict() for a in self.attempts],
            "warnings": list(self.warnings),
            "missing_inputs": list(self.missing_inputs),
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "raw_payload": self.raw_payload,
            "source_metadata": self.source_metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> OrchestrationResult:
        """Reconstitutes result from cache."""
        return cls(
            observation_type=data["observation_type"],
            status=DataStatus(data["status"]),
            confidence=DataConfidence.from_dict(data["confidence"]),
            data=data.get("data", {}),
            selected_provider=data.get("selected_provider"),
            fallback_used=data.get("fallback_used", False),
            is_stale_fallback=data.get("is_stale_fallback", False),
            is_proxy=data.get("is_proxy", False),
            effective_date=date.fromisoformat(data["effective_date"]) if data.get("effective_date") else None,
            published_at=datetime.fromisoformat(data["published_at"]) if data.get("published_at") else None,
            observed_at=datetime.fromisoformat(data["observed_at"]) if data.get("observed_at") else None,
            retrieved_at=datetime.fromisoformat(data["retrieved_at"]) if data.get("retrieved_at") else None,
            canonical_instrument_id=UUID(data["canonical_instrument_id"]) if data.get("canonical_instrument_id") else None,
            provider_symbol=data.get("provider_symbol"),
            attempts=[ProviderAttempt.from_dict(a) for a in data.get("attempts", [])],
            warnings=list(data.get("warnings", [])),
            missing_inputs=list(data.get("missing_inputs", [])),
            provenance=ProviderProvenance.from_dict(data["provenance"]) if data.get("provenance") else None,
            raw_payload=data.get("raw_payload"),
            source_metadata=data.get("source_metadata", {}),
        )

    def to_normalized_observation(
        self,
        asset_class: AssetClass,
        instrument_type: InstrumentType,
        currency: Currency,
        snapshot_id: Optional[UUID] = None,
    ) -> Optional[NormalizedObservationRecord]:
        """
        Maps orchestration result to canonical storage record.
        Strict rule: If snapshot_id is not provided, leaves snapshot_id as None (NO random UUID fabrication).
        """
        if not self.is_available or self.canonical_instrument_id is None or self.effective_date is None:
            return None

        return NormalizedObservationRecord(
            snapshot_id=snapshot_id,  # Hardened: None if not persisted yet
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
            observed_at=self.observed_at or self.retrieved_at or datetime.now(timezone.utc),
            missing_inputs=self.missing_inputs,
            warnings=self.warnings,
            source_refs=[self.provenance.to_source_ref()] if self.provenance else [],
        )

    def to_macro_observation(
        self,
        series_key: str,
        unit: Any,
        frequency: Any,
        snapshot_id: Optional[UUID] = None,
        supersedes_record_id: Optional[UUID] = None,
    ) -> Optional[Any]:
        """
        Maps macro orchestration result to MacroObservationRecord.
        Preserves vintage_date, source_available_date, and origin source metadata.
        Strict: No timestamp, source_tier, or precision fabrication.
        """
        from backend.engine.private.macro.models import MacroObservationRecord
        from backend.engine.private.macro.registry import MacroSeriesRegistry

        if not self.is_available or self.effective_date is None:
            return None

        if self.retrieved_at is None:
            raise ValueError(f"Cannot map to MacroObservationRecord without retrieved_at timestamp (series={series_key}).")

        val = self.data.get("value")
        if val is None and self.data:
            vals = [v for v in self.data.values() if isinstance(v, (int, float))]
            if len(vals) == 1:
                val = float(vals[0])

        src_meta = self.source_metadata or {}
        prov_meta = self.provenance.metadata if self.provenance else {}

        vintage_d = src_meta.get("vintage_date") or prov_meta.get("vintage_date")
        if isinstance(vintage_d, str):
            vintage_d = date.fromisoformat(vintage_d)

        src_avail_d = src_meta.get("source_available_date") or prov_meta.get("source_available_date")
        if isinstance(src_avail_d, str):
            src_avail_d = date.fromisoformat(src_avail_d)

        # Availability precision strictly tied to source_available_date
        avail_precision = None
        if src_avail_d is not None:
            avail_precision = src_meta.get("availability_precision") or prov_meta.get("availability_precision")

        # Source Tier resolution without silent TIER_1 default
        if self.provenance and self.provenance.source_quality:
            resolved_tier = self.provenance.source_quality
        else:
            reg_def = MacroSeriesRegistry.get(series_key)
            if reg_def:
                resolved_tier = reg_def.source_tier
            else:
                raise ValueError(f"Cannot resolve source_tier for macro series '{series_key}'.")

        return MacroObservationRecord(
            series_key=series_key,
            effective_date=self.effective_date,
            value=val,
            unit=unit,
            frequency=frequency,
            data_status=self.status,
            confidence_level=self.confidence.level,
            source_tier=resolved_tier,
            retrieved_at=self.retrieved_at,
            published_at=self.published_at,
            observed_at=self.observed_at or self.retrieved_at,
            source_available_date=src_avail_d,
            availability_precision=avail_precision,
            vintage_date=vintage_d,
            origin_source=src_meta.get("origin_source") or prov_meta.get("origin_source"),
            release_name=src_meta.get("release_name") or prov_meta.get("release_name"),
            snapshot_id=snapshot_id,
            supersedes_record_id=supersedes_record_id,
            warnings=list(self.warnings),
            source_ref=self.provenance.to_source_ref() if self.provenance else None,
            raw_payload=self.raw_payload,
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

        cache_key = context.generate_cache_key()

        # 1. Fresh Cache Check (Bypassed if force_refresh=True or is_historical=True)
        if not context.force_refresh and not context.is_historical:
            cached_data = cache_get(cache_key)
            if cached_data and isinstance(cached_data, dict):
                try:
                    reconstituted = OrchestrationResult.from_dict(cached_data)
                    # Check that cached observation is still fresh under policy
                    if policy.staleness_policy.is_acceptable(
                        effective_date=reconstituted.effective_date,
                        published_at=reconstituted.published_at,
                        observed_at=reconstituted.observed_at,
                        retrieved_at=reconstituted.retrieved_at,
                        as_of_time=context.as_of_time,
                    ):
                        return reconstituted
                except Exception as e:
                    logger.warning(f"Failed to deserialize cached orchestration result: {e}")

        # 2. Iterate through ordered provider fallback chain
        for idx, provider_name in enumerate(policy.ordered_provider_names):
            provider = self._providers.get(provider_name)
            is_fallback = idx > 0

            # Guard: Unregistered provider
            if not provider:
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        attempt_number=1,
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
                        attempt_number=1,
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
                        attempt_number=1,
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
                        attempt_number=1,
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

            # Guard: Lookahead Check for Historical Requests (Directive 1)
            if context.is_historical and context.as_of_time:
                as_of_d = context.as_of_time.date()
                if response.effective_date and response.effective_date > as_of_d:
                    attempts.append(
                        ProviderAttempt(
                            provider_name=provider_name,
                            attempt_number=1,
                            success=False,
                            failure_type="LOOKAHEAD_REJECTED",
                            message=f"Effective date {response.effective_date} is after requested as_of date {as_of_d}.",
                            latency_ms=t_elapsed_ms,
                        )
                    )
                    continue
                if response.published_at and response.published_at > context.as_of_time:
                    attempts.append(
                        ProviderAttempt(
                            provider_name=provider_name,
                            attempt_number=1,
                            success=False,
                            failure_type="LOOKAHEAD_REJECTED",
                            message=f"Published timestamp {response.published_at} is after requested as_of {context.as_of_time}.",
                            latency_ms=t_elapsed_ms,
                        )
                    )
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
                        attempt_number=1,
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

            if has_critical_missing and idx < len(policy.ordered_provider_names) - 1:
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider_name,
                        attempt_number=1,
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

            # 6. Assess Explainable Confidence (with FreshnessBasis & calculation_fields)
            confidence = ConfidenceAssessmentService.assess(
                source_tier=provider.source_quality,
                data_status=status,
                effective_date=response.effective_date,
                published_at=response.published_at,
                observed_at=response.observed_at,
                retrieved_at=response.retrieved_at,
                as_of_time=context.as_of_time,
                required_fields=policy.required_fields,
                optional_fields=policy.optional_fields,
                present_fields=present_fields,
                calculation_fields=policy.calculation_fields,
                field_criticality=policy.field_criticality,
                freshness_basis=policy.freshness_basis,
                warnings=all_warnings,
                is_fallback=is_fallback,
                is_proxy=is_proxy,
                max_staleness_days=policy.max_staleness_seconds // 86400,
            )

            # Record success attempt
            attempts.append(
                ProviderAttempt(
                    provider_name=provider_name,
                    attempt_number=1,
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
                observed_at=response.observed_at or now_utc,
                retrieved_at=response.retrieved_at,
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
                attempts=attempts,
                warnings=all_warnings,
                missing_inputs=all_missing,
                provenance=provenance,
                raw_payload=response.raw,
                source_metadata=getattr(response, "source_metadata", {}),
            )

            # Save in stale cache store & live Redis cache
            if status != DataStatus.UNAVAILABLE:
                self._stale_cache_store[cache_key] = result
                if not context.is_historical:
                    cache_set(cache_key, result.to_dict(), ttl=policy.max_staleness_seconds)

            return result

        # 7. All providers in chain failed — Check acceptable stale cache (Only if allow_stale=True)
        if policy.allow_stale and cache_key in self._stale_cache_store:
            cached_res = self._stale_cache_store[cache_key]
            if policy.staleness_policy.is_acceptable(
                effective_date=cached_res.effective_date,
                published_at=cached_res.published_at,
                observed_at=cached_res.observed_at,
                retrieved_at=cached_res.retrieved_at,
                as_of_time=context.as_of_time,
            ):
                stale_warnings = list(cached_res.warnings) + ["All live providers failed; using acceptable stale cached data."]
                stale_conf = ConfidenceAssessmentService.assess(
                    source_tier=cached_res.provenance.source_quality if cached_res.provenance else SourceTier.TIER_5_PROXY,
                    data_status=DataStatus.STALE,
                    effective_date=cached_res.effective_date,
                    published_at=cached_res.published_at,
                    observed_at=cached_res.observed_at,
                    retrieved_at=cached_res.retrieved_at,
                    as_of_time=context.as_of_time,
                    required_fields=policy.required_fields,
                    optional_fields=policy.optional_fields,
                    present_fields=list(cached_res.data.keys()),
                    calculation_fields=policy.calculation_fields,
                    field_criticality=policy.field_criticality,
                    freshness_basis=policy.freshness_basis,
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
                reasons.append(f"{a.provider_name} (Attempt {a.attempt_number}): {a.failure_type} ({a.message or 'No details'})")

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
        """
        Executes fetch with bounded retries on transient errors.
        Records every attempt in diagnostic attempts list.
        """
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
                            attempt_number=attempt_no,
                            success=False,
                            failure_type="UNAVAILABLE",
                            message=f"Provider returned UNAVAILABLE: {', '.join(response.warnings)}",
                            latency_ms=(time.perf_counter() - t_start) * 1000.0,
                        )
                    )
                    return None
                return response
            except (asyncio.TimeoutError, ProviderTimeoutError) as e:
                latency = (time.perf_counter() - t_start) * 1000.0
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider.provider_name,
                        attempt_number=attempt_no,
                        success=False,
                        failure_type="TIMEOUT",
                        message=f"Attempt {attempt_no} timed out: {str(e)}",
                        latency_ms=latency,
                    )
                )
                if attempt_no < max_attempts:
                    await asyncio.sleep(retry_policy.backoff_factor * attempt_no)
                else:
                    return None
            except ProviderRateLimitError as e:
                latency = (time.perf_counter() - t_start) * 1000.0
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider.provider_name,
                        attempt_number=attempt_no,
                        success=False,
                        failure_type="RATE_LIMIT",
                        message=str(e),
                        latency_ms=latency,
                    )
                )
                if attempt_no < max_attempts:
                    wait_time = e.retry_after_seconds or (retry_policy.backoff_factor * attempt_no)
                    await asyncio.sleep(wait_time)
                else:
                    return None
            except (ProviderAuthenticationError, ProviderPermissionError, ProviderInvalidSymbolError, ProviderSchemaError, NonRetryableProviderError) as e:
                # Fast-fail non-retryable errors
                latency = (time.perf_counter() - t_start) * 1000.0
                failure_type = e.__class__.__name__.replace("Provider", "").replace("Error", "").upper()
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider.provider_name,
                        attempt_number=attempt_no,
                        success=False,
                        failure_type=failure_type or "NON_RETRYABLE",
                        message=f"Non-retryable error: {str(e)}",
                        latency_ms=latency,
                    )
                )
                return None
            except Exception as e:
                latency = (time.perf_counter() - t_start) * 1000.0
                attempts.append(
                    ProviderAttempt(
                        provider_name=provider.provider_name,
                        attempt_number=attempt_no,
                        success=False,
                        failure_type="FETCH_ERROR",
                        message=str(e),
                        latency_ms=latency,
                    )
                )
                if attempt_no < max_attempts:
                    await asyncio.sleep(retry_policy.backoff_factor * attempt_no)
                else:
                    return None
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
