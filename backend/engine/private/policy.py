"""
backend/engine/private/policy.py
==================================
Configurable Data Sourcing, Fallback, Staleness, and Retry Policies.

Core Principles:
    - Provider priority is NOT hardcoded in application logic.
    - Sourcing rules are declared per observation_type via explicit policy objects.
    - Non-retryable errors (auth, schema mismatch, invalid symbol) fail fast.
    - Stale cache is ONLY used if explicitly permitted by StalenessPolicy.
    - Proxy providers are strictly forbidden unless explicit in SourcePolicy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Set

from backend.engine.private.domain import DataCriticality, SourceTier


@dataclass
class RetryPolicy:
    """
    Governs retry behavior for transient network failures.
    """
    max_attempts: int = 3
    backoff_factor: float = 0.5
    timeout_seconds: float = 10.0
    retryable_status_codes: Set[int] = field(
        default_factory=lambda: {429, 500, 502, 503, 504}
    )

    def is_retryable_status(self, http_status: Optional[int]) -> bool:
        if http_status is None:
            return True  # Connection drop or timeout is retryable
        return http_status in self.retryable_status_codes


@dataclass
class StalenessPolicy:
    """
    Governs acceptable age thresholds for historical and cached observations.
    """
    allow_stale_fallback: bool = True
    max_staleness_seconds: int = 86400 * 3  # Default 3 days

    def is_acceptable(
        self,
        effective_date: Optional[date],
        as_of_time: Optional[datetime] = None,
    ) -> bool:
        if not self.allow_stale_fallback or effective_date is None:
            return False
        ref_date = (as_of_time or datetime.now(timezone.utc)).date()
        age_days = (ref_date - effective_date).days
        if age_days < 0:
            return True  # Current day or future observation
        return (age_days * 86400) <= self.max_staleness_seconds


@dataclass
class SourcePolicy:
    """
    Declarative policy defining the provider chain, required fields, and tolerances
    for a given observation type.
    """
    observation_type: str
    ordered_provider_names: List[str]
    allow_stale: bool = True
    max_staleness_seconds: int = 86400 * 3
    allow_proxy: bool = False
    minimum_source_tier: SourceTier = SourceTier.TIER_5_PROXY
    required_fields: List[str] = field(default_factory=list)
    optional_fields: List[str] = field(default_factory=list)
    field_criticality: Dict[str, DataCriticality] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def staleness_policy(self) -> StalenessPolicy:
        return StalenessPolicy(
            allow_stale_fallback=self.allow_stale,
            max_staleness_seconds=self.max_staleness_seconds,
        )
