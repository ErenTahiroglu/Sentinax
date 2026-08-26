"""
backend/engine/private/confidence.py
======================================
Explainable Data Confidence Assessment Model.

Core Principles:
    - Data Confidence is NOT an investment recommendation score.
    - Evaluates 4 distinct dimensions: freshness, source_quality, coverage, consistency.
    - calculation_coverage tracks the proportion of required inputs available for a specific formula.
    - Categorical level (HIGH, MEDIUM, LOW, NONE) is derived without false precision.
    - Every degradation includes human-readable explanatory reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from backend.engine.private.domain import (
    DataConfidenceLevel,
    DataCriticality,
    DataStatus,
    SourceTier,
)


@dataclass
class DataConfidence:
    """
    Explainable confidence metrics for a normalized observation or analysis output.
    """
    level: DataConfidenceLevel
    freshness: float            # 0.0 to 1.0: How current the observation is relative to expected frequency
    source_quality: float       # 0.0 to 1.0: Derived from SourceTier authority
    coverage: float             # 0.0 to 1.0: Proportion of required/optional fields populated
    consistency: float          # 0.0 to 1.0: Absence of anomalies or cross-provider conflicts
    calculation_coverage: float # 0.0 to 1.0: Proportion of inputs available for a specific formula
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "freshness": round(self.freshness, 3),
            "source_quality": round(self.source_quality, 3),
            "coverage": round(self.coverage, 3),
            "consistency": round(self.consistency, 3),
            "calculation_coverage": round(self.calculation_coverage, 3),
            "reasons": list(self.reasons),
        }

    @classmethod
    def unavailable(cls, reasons: Optional[List[str]] = None) -> DataConfidence:
        """Helper for completely missing/unavailable data."""
        return cls(
            level=DataConfidenceLevel.NONE,
            freshness=0.0,
            source_quality=0.0,
            coverage=0.0,
            consistency=1.0,
            calculation_coverage=0.0,
            reasons=reasons or ["No usable data available from any provider."],
        )


class ConfidenceAssessmentService:
    """
    Evaluates explainable data confidence for observations and computed metrics.
    """

    # SourceTier base weight scores
    TIER_WEIGHTS = {
        SourceTier.TIER_1_REGULATORY: 1.0,
        SourceTier.TIER_2_EXCHANGE: 0.85,
        SourceTier.TIER_3_AGGREGATOR: 0.70,
        SourceTier.TIER_4_DERIVED: 0.50,
        SourceTier.TIER_5_PROXY: 0.30,
    }

    @classmethod
    def assess(
        cls,
        source_tier: SourceTier,
        data_status: DataStatus,
        effective_date: Optional[date],
        retrieved_at: Optional[datetime],
        as_of_time: Optional[datetime],
        required_fields: List[str],
        optional_fields: List[str],
        present_fields: List[str],
        field_criticality: Optional[Dict[str, DataCriticality]] = None,
        warnings: Optional[List[str]] = None,
        is_fallback: bool = False,
        is_proxy: bool = False,
        max_staleness_days: int = 3,
        discrepancy_penalty: float = 0.0,
    ) -> DataConfidence:
        reasons: List[str] = []
        warnings = warnings or []
        crit_map = field_criticality or {}

        # 1. Source Quality Component (0.0 to 1.0)
        source_quality_score = cls.TIER_WEIGHTS.get(source_tier, 0.5)
        if is_fallback:
            source_quality_score *= 0.85
            reasons.append("Primary provider unavailable; fallback source used.")
        if is_proxy:
            source_quality_score *= 0.60
            reasons.append("Proxy observation used; not exact security observation.")

        # 2. Coverage & Calculation Coverage Component (0.0 to 1.0)
        req_set = set(required_fields)
        opt_set = set(optional_fields)
        pres_set = set(present_fields)

        missing_req = req_set - pres_set
        missing_opt = opt_set - pres_set

        total_fields_count = len(req_set) + len(opt_set)
        if total_fields_count == 0:
            coverage_score = 1.0
            calc_cov = 1.0
        else:
            # Required fields weight 70%, optional fields weight 30%
            req_cov = 1.0 if not req_set else (len(req_set & pres_set) / len(req_set))
            opt_cov = 1.0 if not opt_set else (len(opt_set & pres_set) / len(opt_set))
            coverage_score = (req_cov * 0.7) + (opt_cov * 0.3)
            calc_cov = len(pres_set & (req_set | opt_set)) / total_fields_count

        if missing_req:
            reasons.append(f"Missing required fields: {', '.join(sorted(missing_req))}.")
        if missing_opt:
            reasons.append(f"Missing optional fields: {len(missing_opt)}/{len(opt_set)} omitted.")

        # 3. Freshness Component (0.0 to 1.0)
        ref_time = as_of_time or datetime.now(timezone.utc)
        freshness_score = 1.0

        if effective_date:
            ref_date = ref_time.date() if isinstance(ref_time, datetime) else ref_time
            age_days = (ref_date - effective_date).days
            if age_days < 0:
                age_days = 0 # Lookahead protection handled elsewhere

            if age_days == 0:
                freshness_score = 1.0
            elif age_days <= max_staleness_days:
                freshness_score = max(0.5, 1.0 - (age_days * (0.5 / max_staleness_days)))
                if age_days > 1:
                    reasons.append(f"Data is {age_days} days old.")
            else:
                freshness_score = max(0.1, 0.5 - ((age_days - max_staleness_days) * 0.05))
                reasons.append(f"Data is stale ({age_days} days old, max acceptable: {max_staleness_days} days).")
        elif data_status == DataStatus.UNAVAILABLE:
            freshness_score = 0.0
            reasons.append("No effective date present.")

        # 4. Consistency Component (0.0 to 1.0)
        consistency_score = max(0.0, 1.0 - discrepancy_penalty)
        if warnings:
            # Deduct for warnings
            warning_penalty = min(0.4, len(warnings) * 0.1)
            consistency_score = max(0.1, consistency_score - warning_penalty)
            for w in warnings:
                reasons.append(f"Warning: {w}")

        if discrepancy_penalty > 0:
            reasons.append(f"Cross-source discrepancy detected (penalty: {round(discrepancy_penalty, 2)}).")

        # 5. Composite Score & Categorical Level Derivation
        # Weights: Source Quality (30%), Coverage (30%), Freshness (25%), Consistency (15%)
        composite = (
            (source_quality_score * 0.30)
            + (coverage_score * 0.30)
            + (freshness_score * 0.25)
            + (consistency_score * 0.15)
        )

        # Critical missing fields immediately cap confidence
        has_critical_missing = any(
            crit_map.get(f) == DataCriticality.CRITICAL for f in missing_req
        )

        if data_status == DataStatus.UNAVAILABLE or has_critical_missing:
            level = DataConfidenceLevel.NONE
            if has_critical_missing:
                reasons.append("Critical calculation input is missing.")
        elif data_status == DataStatus.STALE or composite < 0.40:
            level = DataConfidenceLevel.LOW
        elif data_status == DataStatus.PARTIAL or data_status == DataStatus.DEGRADED or composite < 0.75:
            level = DataConfidenceLevel.MEDIUM
        else:
            level = DataConfidenceLevel.HIGH

        return DataConfidence(
            level=level,
            freshness=freshness_score,
            source_quality=source_quality_score,
            coverage=coverage_score,
            consistency=consistency_score,
            calculation_coverage=calc_cov,
            reasons=reasons,
        )
