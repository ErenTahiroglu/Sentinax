"""
backend/engine/private/policy.py
==================================
Configurable Data Sourcing, Fallback, Staleness, and Retry Policies.

Core Principles:
    - Provider priority is NOT hardcoded in application logic.
    - Stale data is STRICTLY opt-in (default: allow_stale=False).
    - Future-dated data (relative to as_of boundary) is rejected (no lookahead).
    - Sourcing rules are declared per observation_type via explicit policy objects.
    - Non-retryable errors fail fast.
    - Calculation fields explicitly separate mathematical input coverage from optional metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Set

from backend.engine.private.domain import (
    DataCriticality,
    FreshnessBasis,
    SourceTier,
)


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
    Stale fallback is EXPLICITLY OPT-IN (default: False).
    """
    allow_stale_fallback: bool = False  # Hardened: Opt-in only
    max_staleness_seconds: int = 86400 * 3  # Default 3 days
    freshness_basis: FreshnessBasis = FreshnessBasis.EFFECTIVE_DATE

    def is_acceptable(
        self,
        effective_date: Optional[date],
        published_at: Optional[datetime] = None,
        observed_at: Optional[datetime] = None,
        retrieved_at: Optional[datetime] = None,
        as_of_time: Optional[datetime] = None,
    ) -> bool:
        if not self.allow_stale_fallback:
            return False

        # Select timestamp based on configured FreshnessBasis
        if self.freshness_basis == FreshnessBasis.PUBLISHED_AT:
            eval_dt = published_at or observed_at or retrieved_at
            eval_date = eval_dt.date() if eval_dt else effective_date
        elif self.freshness_basis == FreshnessBasis.OBSERVED_AT:
            eval_dt = observed_at or retrieved_at
            eval_date = eval_dt.date() if eval_dt else effective_date
        elif self.freshness_basis == FreshnessBasis.RETRIEVED_AT:
            eval_dt = retrieved_at
            eval_date = eval_dt.date() if eval_dt else effective_date
        else:
            eval_date = effective_date

        if eval_date is None:
            return False

        ref_date = (as_of_time or datetime.now(timezone.utc)).date()
        age_days = (ref_date - eval_date).days

        # Lookahead protection: Future data relative to as_of is NEVER acceptable stale data
        if age_days < 0:
            return False

        return (age_days * 86400) <= self.max_staleness_seconds


@dataclass
class SourcePolicy:
    """
    Declarative policy defining the provider chain, required fields, and tolerances
    for a given observation type.
    """
    observation_type: str
    ordered_provider_names: List[str]
    allow_stale: bool = False  # Hardened: Opt-in only
    max_staleness_seconds: int = 86400 * 3
    allow_proxy: bool = False  # Hardened: Opt-in only
    minimum_source_tier: SourceTier = SourceTier.TIER_5_PROXY
    freshness_basis: FreshnessBasis = FreshnessBasis.EFFECTIVE_DATE
    required_fields: List[str] = field(default_factory=list)
    optional_fields: List[str] = field(default_factory=list)
    calculation_fields: Optional[List[str]] = None  # Fields strictly required for calculation
    field_criticality: Dict[str, DataCriticality] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def staleness_policy(self) -> StalenessPolicy:
        return StalenessPolicy(
            allow_stale_fallback=self.allow_stale,
            max_staleness_seconds=self.max_staleness_seconds,
            freshness_basis=self.freshness_basis,
        )
