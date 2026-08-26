"""
backend/engine/private/confidence.py
======================================
Explainable Data Confidence Assessment Model.

Core Principles:
    - Data Confidence is NOT an investment recommendation score.
    - Evaluates 4 distinct dimensions: freshness, source_quality, coverage, consistency.
    - calculation_coverage tracks proportion of inputs strictly required for calculation (ignores irrelevant optional display fields).
    - Lookahead protection: future-dated data relative to as-of boundary receives 0.0 freshness and NONE confidence.
    - FreshnessBasis configurable (EFFECTIVE_DATE, PUBLISHED_AT, OBSERVED_AT, RETRIEVED_AT).
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
    FreshnessBasis,
    SourceTier,
)


@dataclass
class DataConflict:
    """Structured representation of discrepancy across data sources."""
    severity: str  # 'LOW', 'MEDIUM', 'HIGH'
    conflicting_providers: List[str]
    field_discrepancies: Dict[str, Any]
    reason: str


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
    calculation_coverage: float # 0.0 to 1.0: Proportion of inputs available strictly for the calculation
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
    def from_dict(cls, data: Dict[str, Any]) -> DataConfidence:
        return cls(
            level=DataConfidenceLevel(data["level"]),
            freshness=data.get("freshness", 0.0),
            source_quality=data.get("source_quality", 0.0),
            coverage=data.get("coverage", 0.0),
            consistency=data.get("consistency", 1.0),
            calculation_coverage=data.get("calculation_coverage", 0.0),
            reasons=list(data.get("reasons", [])),
        )

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
        published_at: Optional[datetime],
        observed_at: Optional[datetime],
        retrieved_at: Optional[datetime],
        as_of_time: Optional[datetime],
        required_fields: List[str],
        optional_fields: List[str],
        present_fields: List[str],
        calculation_fields: Optional[List[str]] = None,
        field_criticality: Optional[Dict[str, DataCriticality]] = None,
        freshness_basis: FreshnessBasis = FreshnessBasis.EFFECTIVE_DATE,
        warnings: Optional[List[str]] = None,
        conflicts: Optional[List[DataConflict]] = None,
        is_fallback: bool = False,
        is_proxy: bool = False,
        max_staleness_days: Optional[int] = 3,
    ) -> DataConfidence:
        reasons: List[str] = []
        warnings = warnings or []
        crit_map = field_criticality or {}
        is_lookahead_violation = False

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
        else:
            # Required fields weight 70%, optional fields weight 30%
            req_cov = 1.0 if not req_set else (len(req_set & pres_set) / len(req_set))
            opt_cov = 1.0 if not opt_set else (len(opt_set & pres_set) / len(opt_set))
            coverage_score = (req_cov * 0.7) + (opt_cov * 0.3)

        # Calculation coverage specifically evaluates required calculation inputs (Directive 10)
        calc_target_fields = set(calculation_fields) if calculation_fields is not None else req_set
        if not calc_target_fields:
            calc_cov = 1.0
        else:
            calc_cov = len(calc_target_fields & pres_set) / len(calc_target_fields)

        if missing_req:
            reasons.append(f"Missing required fields: {', '.join(sorted(missing_req))}.")
        if missing_opt:
            reasons.append(f"Missing optional fields: {len(missing_opt)}/{len(opt_set)} omitted.")

        # 3. Freshness Component (0.0 to 1.0) & Lookahead Protection (Directive 1 & 2)
        ref_time = as_of_time or datetime.now(timezone.utc)
        freshness_score = 1.0

        # Determine reference timestamp based on configured FreshnessBasis
        if freshness_basis == FreshnessBasis.PUBLISHED_AT:
            eval_dt = published_at or observed_at or retrieved_at
            eval_date = eval_dt.date() if eval_dt else effective_date
        elif freshness_basis == FreshnessBasis.OBSERVED_AT:
            eval_dt = observed_at or retrieved_at
            eval_date = eval_dt.date() if eval_dt else effective_date
        elif freshness_basis == FreshnessBasis.RETRIEVED_AT:
            eval_dt = retrieved_at
            eval_date = eval_dt.date() if eval_dt else effective_date
        else: # EFFECTIVE_DATE
            eval_date = effective_date
            eval_dt = None

        if eval_date:
            ref_date = ref_time.date() if isinstance(ref_time, datetime) else ref_time
            age_days = (ref_date - eval_date).days

            # Lookahead check: observation is in future relative to as_of boundary!
            if age_days < 0:
                is_lookahead_violation = True
                freshness_score = 0.0
                reasons.append(f"Observation occurs after requested as-of boundary (future data lookahead: {eval_date} > {ref_date}).")
            elif max_staleness_days is None:
                # Event-driven series (e.g. policy rate decision): valid until next official decision
                freshness_score = 1.0
            elif age_days == 0:
                freshness_score = 1.0
            elif age_days <= max_staleness_days:
                freshness_score = max(0.5, 1.0 - (age_days * (0.5 / max_staleness_days)))
                if age_days > 1:
                    reasons.append(f"Data is {age_days} days old based on {freshness_basis.value}.")
            else:
                freshness_score = max(0.1, 0.5 - ((age_days - max_staleness_days) * 0.05))
                reasons.append(f"Data is stale ({age_days} days old, max acceptable: {max_staleness_days} days).")
        elif data_status == DataStatus.UNAVAILABLE:
            freshness_score = 0.0
            reasons.append("No effective date/timestamp present.")

        # 4. Consistency Component (0.0 to 1.0)
        consistency_score = 1.0
        if conflicts:
            for c in conflicts:
                pen = 0.4 if c.severity == "HIGH" else 0.2
                consistency_score = max(0.1, consistency_score - pen)
                reasons.append(f"Conflict ({c.severity}): {c.reason}")

        if warnings:
            warning_penalty = min(0.4, len(warnings) * 0.1)
            consistency_score = max(0.1, consistency_score - warning_penalty)
            for w in warnings:
                reasons.append(f"Warning: {w}")

        # 5. Composite Score & Categorical Level Derivation
        composite = (
            (source_quality_score * 0.30)
            + (coverage_score * 0.30)
            + (freshness_score * 0.25)
            + (consistency_score * 0.15)
        )

        has_critical_missing = any(
            crit_map.get(f) == DataCriticality.CRITICAL for f in missing_req
        )

        if data_status == DataStatus.UNAVAILABLE or has_critical_missing or is_lookahead_violation:
            level = DataConfidenceLevel.NONE
            if has_critical_missing:
                reasons.append("Critical calculation input is missing.")
        elif data_status == DataStatus.STALE or freshness_score <= 0.3 or composite < 0.40:
            level = DataConfidenceLevel.LOW
        elif data_status == DataStatus.PARTIAL or data_status == DataStatus.DEGRADED or freshness_score < 0.6 or composite < 0.75:
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
