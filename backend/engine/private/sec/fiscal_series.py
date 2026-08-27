"""
backend/engine/private/sec/fiscal_series.py
=============================================
SEC EDGAR Phase 8B.3A / 8B.3A.5 / 8B.3A.6:
PIT Fiscal Series Assembly, Fiscal Anchor Semantics, Central Period-Bound Reuse & Fail-Closed Identity.

Core Invariants:
    - Pure Economic Series Assembly: Groups verified winner results by CIK, canonical concept, unit,
      resolution mode, as_of date, and evaluation snapshot state.
    - No Date Fabrication: Missing economic dates are NEVER replaced with epoch or sentinel dates.
      Selected winner candidates lacking economic_end_date are excluded from normal series points and logged.
    - Structured Series Conflict: Conflicting values for identical economic periods are recorded as structured
      SECFiscalSeriesConflict objects and mark the series status as CONFLICTED.
    - Conflict Cannot Be Bypassed: A target quarter cannot bypass a conflicted standalone or operand interval
      to fall back to YTD derivation or declare missing operands; returns SERIES_CONFLICT.
    - Fiscal Start Anchor Semantics: Standalone quarter starts (Apr 1 for Q2, Jul 1 for Q3) are NOT fiscal-year starts.
      Fiscal start anchors are derived strictly from cumulative/start-of-year evidence:
        * Q1 QUARTER_DURATION start_date
        * Q2 YTD_DURATION start_date
        * Q3 YTD_DURATION start_date
        * ANNUAL_DURATION start_date
    - Central Period-Bound Reuse: Reuses centralized inclusive duration bounds (QUARTER_MIN_DAYS/MAX_DAYS,
      YTD_6M_MIN_DAYS/MAX_DAYS, YTD_9M_MIN_DAYS/MAX_DAYS) from period_context.py.
    - Inclusive Duration Convention: duration_days = (end_date - start_date).days + 1.
    - Fail-Closed Duration Validation: duration_days=None or missing dates cannot positively prove period identity.
    - Standalone Relational Identity: Standalone Q2/Q3 prefer strong relational proof against cumulative endpoints
      (e.g. Q2 standalone end == Q2_YTD end) before falling back to date ordinal proofs.
    - Snapshot State Consistency: All operands in a single derivation chain MUST originate from the same
      logical CompanyFacts evaluation snapshot state (identical retrieved_at and payload_hash).
    - Mode & As-Of Isolation: CURRENT_REPORTED and SYSTEM_AS_OF points cannot be mixed; SYSTEM_AS_OF operands
      must share identical as_of timestamps.
    - Duration Concepts Only: Instant facts (Balance Sheet, Shares Outstanding) are ineligible for quarter subtraction.
    - Q4 & TTM Out-Of-Scope: Explicitly return UNSUPPORTED_PERIOD in Phase 8B.3A.
    - No Arithmetic Yet: Pure eligibility classification; no subtraction or numerical derivations performed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.concepts import (
    CanonicalSECConceptDefinition,
    PeriodType,
    get_initial_canonical_concept_definitions,
)
from backend.engine.private.sec.period_context import (
    ANNUAL_MAX_DAYS,
    ANNUAL_MIN_DAYS,
    QUARTER_MAX_DAYS,
    QUARTER_MIN_DAYS,
    SECEconomicPeriodKind,
    YTD_6M_MAX_DAYS,
    YTD_6M_MIN_DAYS,
    YTD_9M_MAX_DAYS,
    YTD_9M_MIN_DAYS,
)
from backend.engine.private.sec.winner_resolver import (
    SECWinnerResolutionMode,
    SECWinnerResolutionResult,
    SECWinnerStatus,
)


_CANONICAL_CONCEPTS_MAP: Dict[str, CanonicalSECConceptDefinition] = {
    defn.canonical_concept: defn
    for defn in get_initial_canonical_concept_definitions()
}


class SECFiscalSeriesStatus(Enum):
    """Overall status of an assembled fiscal series."""
    VALID = "valid"
    PARTIAL = "partial"
    CONFLICTED = "conflicted"


class SECDerivationEligibilityStatus(Enum):
    """Detailed deterministic status of quarter derivation eligibility."""
    ORIGINAL_AVAILABLE = "original_available"
    ELIGIBLE = "eligible"
    MISSING_OPERAND = "missing_operand"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    MODE_MISMATCH = "mode_mismatch"
    AS_OF_MISMATCH = "as_of_mismatch"
    CONCEPT_MISMATCH = "concept_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    FISCAL_START_MISMATCH = "fiscal_start_mismatch"
    PERIOD_SEQUENCE_INVALID = "period_sequence_invalid"
    SEMANTIC_QUALITY_RISK = "semantic_quality_risk"
    SERIES_CONFLICT = "series_conflict"
    AMBIGUOUS_FISCAL_CHAIN = "ambiguous_fiscal_chain"
    PERIOD_IDENTITY_UNRESOLVED = "period_identity_unresolved"
    UNSUPPORTED_PERIOD = "unsupported_period"
    UNAVAILABLE = "unavailable"


@dataclass
class SECFiscalSeriesConflict:
    """
    Structured conflict record for a period interval with conflicting winner values.
    """
    economic_period_kind: SECEconomicPeriodKind
    start_date: Optional[date]
    end_date: date
    values: List[Decimal]
    winner_result_ids: List[UUID]
    accession_numbers: List[str]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "economic_period_kind": self.economic_period_kind.value,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat(),
            "values": [str(v) for v in self.values],
            "winner_result_ids": [str(wid) for wid in self.winner_result_ids],
            "accession_numbers": self.accession_numbers,
            "reason": self.reason,
        }


@dataclass
class SECFiscalSeriesPoint:
    """
    Individual verified data point in a fiscal series.
    """
    winner_result: SECWinnerResolutionResult
    economic_period_kind: SECEconomicPeriodKind
    start_date: Optional[date]
    end_date: date
    duration_days: Optional[int]
    fiscal_year: Optional[int]
    fiscal_period: Optional[str]
    selected_value: Decimal
    selected_accession: Optional[str]
    selected_filing_id: Optional[UUID]
    evaluation_snapshot_id: Optional[UUID]
    evaluation_snapshot_ids: List[UUID]
    evaluation_snapshot_retrieved_at: Optional[datetime]
    evaluation_snapshot_hash: Optional[str]
    source_concept: Optional[str]
    match_strength: Optional[str]
    selection_confidence: Optional[str]
    is_comparative: bool
    is_amendment: bool
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "economic_period_kind": self.economic_period_kind.value,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat(),
            "duration_days": self.duration_days,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "selected_value": str(self.selected_value),
            "selected_accession": self.selected_accession,
            "selected_filing_id": str(self.selected_filing_id) if self.selected_filing_id else None,
            "evaluation_snapshot_id": str(self.evaluation_snapshot_id) if self.evaluation_snapshot_id else None,
            "evaluation_snapshot_ids": [str(sid) for sid in self.evaluation_snapshot_ids],
            "evaluation_snapshot_retrieved_at": self.evaluation_snapshot_retrieved_at.isoformat() if self.evaluation_snapshot_retrieved_at else None,
            "evaluation_snapshot_hash": self.evaluation_snapshot_hash,
            "source_concept": self.source_concept,
            "match_strength": self.match_strength,
            "selection_confidence": self.selection_confidence,
            "is_comparative": self.is_comparative,
            "is_amendment": self.is_amendment,
            "diagnostics": self.diagnostics,
        }


@dataclass
class SECFiscalSeries:
    """
    Consistent fiscal period series for a single CIK, canonical concept, unit, and resolution mode.
    """
    cik: str
    canonical_concept: str
    unit: str
    resolution_mode: SECWinnerResolutionMode
    as_of: Optional[datetime] = None
    evaluation_snapshot_retrieved_at: Optional[datetime] = None
    evaluation_snapshot_hash: Optional[str] = None
    status: SECFiscalSeriesStatus = SECFiscalSeriesStatus.VALID
    points: List[SECFiscalSeriesPoint] = field(default_factory=list)
    conflicts: List[SECFiscalSeriesConflict] = field(default_factory=list)
    failed_results: List[SECWinnerResolutionResult] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cik": self.cik,
            "canonical_concept": self.canonical_concept,
            "unit": self.unit,
            "resolution_mode": self.resolution_mode.value,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "evaluation_snapshot_retrieved_at": self.evaluation_snapshot_retrieved_at.isoformat() if self.evaluation_snapshot_retrieved_at else None,
            "evaluation_snapshot_hash": self.evaluation_snapshot_hash,
            "status": self.status.value,
            "points": [p.to_dict() for p in self.points],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "failed_results_count": len(self.failed_results),
            "diagnostics": self.diagnostics,
        }


@dataclass
class SECQuarterDerivationEligibility:
    """
    Audit trail and eligibility outcome for deriving a standalone fiscal quarter.
    """
    target_quarter: str
    status: SECDerivationEligibilityStatus
    canonical_concept: str
    unit: str
    left_operand: Optional[SECFiscalSeriesPoint] = None
    right_operand: Optional[SECFiscalSeriesPoint] = None
    expected_formula: Optional[str] = None
    snapshot_hash: Optional[str] = None
    snapshot_retrieved_at: Optional[datetime] = None
    confidence: Optional[str] = None  # "HIGH", "MEDIUM", "LOW"
    basis: str = ""
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_quarter": self.target_quarter,
            "status": self.status.value,
            "canonical_concept": self.canonical_concept,
            "unit": self.unit,
            "left_operand": self.left_operand.to_dict() if self.left_operand else None,
            "right_operand": self.right_operand.to_dict() if self.right_operand else None,
            "expected_formula": self.expected_formula,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_retrieved_at": self.snapshot_retrieved_at.isoformat() if self.snapshot_retrieved_at else None,
            "confidence": self.confidence,
            "basis": self.basis,
            "diagnostics": self.diagnostics,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions: Inclusive Duration & Period Checks
# ─────────────────────────────────────────────────────────────────────────────

def _effective_inclusive_duration(
    start_date: Optional[date],
    end_date: Optional[date],
    duration_days: Optional[int],
) -> Optional[int]:
    """
    Computes effective inclusive duration ((end_date - start_date).days + 1)
    and validates consistency with supplied duration_days if available.
    Returns None if dates are missing.
    """
    if start_date is None or end_date is None:
        return None
    if end_date < start_date:
        return None
    computed = (end_date - start_date).days + 1
    return computed


def _is_valid_quarter_duration(inclusive_days: Optional[int]) -> bool:
    if inclusive_days is None:
        return False
    return QUARTER_MIN_DAYS <= inclusive_days <= QUARTER_MAX_DAYS


def _is_q2_ytd_duration(inclusive_days: Optional[int]) -> bool:
    if inclusive_days is None:
        return False
    return YTD_6M_MIN_DAYS <= inclusive_days <= YTD_6M_MAX_DAYS


def _is_q3_ytd_duration(inclusive_days: Optional[int]) -> bool:
    if inclusive_days is None:
        return False
    return YTD_9M_MIN_DAYS <= inclusive_days <= YTD_9M_MAX_DAYS


def _collect_fiscal_start_anchors(
    series: SECFiscalSeries,
    target_fiscal_year: Optional[int] = None,
    for_quarter: Optional[str] = None,
) -> Set[date]:
    """
    Collects unambiguous fiscal year start anchors from cumulative/start-of-year points.
    CRITICAL: Standalone Q2/Q3 start dates are NEVER fiscal-start anchors.
    """
    anchors: Set[date] = set()

    for p in series.points:
        if p.start_date is None or p.end_date is None:
            continue
        if target_fiscal_year is not None and p.fiscal_year != target_fiscal_year:
            continue

        dur = _effective_inclusive_duration(p.start_date, p.end_date, p.duration_days)
        if dur is None:
            continue

        # 1. Q1 Quarter Anchor (QUARTER_DURATION starting at fiscal start)
        if p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION:
            if _is_valid_quarter_duration(dur):
                # fp if present must not indicate later quarters
                if p.fiscal_period is None or p.fiscal_period.upper() == "Q1":
                    # If evaluating specifically for Q2/Q3, Q1 is still a valid fiscal start anchor
                    anchors.add(p.start_date)

        # 2. Q2 YTD Anchor
        elif p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION:
            if _is_q2_ytd_duration(dur):
                anchors.add(p.start_date)
            elif _is_q3_ytd_duration(dur):
                anchors.add(p.start_date)

        # 3. Annual Duration Anchor
        elif p.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION:
            if ANNUAL_MIN_DAYS <= dur <= ANNUAL_MAX_DAYS:
                anchors.add(p.start_date)

    # Check conflicts for cumulative start anchors
    for c in series.conflicts:
        if c.start_date is None or c.end_date is None:
            continue
        dur = (c.end_date - c.start_date).days + 1
        if c.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION:
            # Only if it matches a Q1 duration
            if _is_valid_quarter_duration(dur):
                # Avoid standalone Q2/Q3 conflict start dates
                if for_quarter == "Q1" or target_fiscal_year is not None:
                    anchors.add(c.start_date)
        elif c.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION:
            if _is_q2_ytd_duration(dur) or _is_q3_ytd_duration(dur):
                anchors.add(c.start_date)

    return anchors


# ─────────────────────────────────────────────────────────────────────────────
# Fiscal Series Assembler
# ─────────────────────────────────────────────────────────────────────────────

class SECFiscalSeriesAssembler:
    """
    Pure assembler of SECWinnerResolutionResults into consistent SECFiscalSeries.
    """

    @classmethod
    def assemble_series(
        cls,
        results: List[SECWinnerResolutionResult],
    ) -> List[SECFiscalSeries]:
        """
        Groups verified winner results into isolated fiscal series.
        Separates by CIK, canonical_concept, unit, resolution_mode, as_of, and snapshot state.
        """
        # Cluster results by series identity key
        clusters: Dict[
            Tuple[str, str, str, SECWinnerResolutionMode, Optional[datetime], Optional[datetime], Optional[str]],
            List[SECWinnerResolutionResult]
        ] = {}

        for res in results:
            cik = normalize_cik(res.cik) if res.cik else ""
            concept = ""
            unit = ""
            if res.selected_candidate:
                concept = res.selected_candidate.canonical_concept
                unit = res.selected_candidate.unit
            elif res.economic_group_key:
                concept = res.economic_group_key[1]
                unit = res.economic_group_key[2]

            mode = res.mode
            as_of = res.as_of
            snap_time = res.evaluation_snapshot_retrieved_at
            snap_hash = res.evaluation_snapshot_hash

            cluster_key = (cik, concept, unit, mode, as_of, snap_time, snap_hash)
            clusters.setdefault(cluster_key, []).append(res)

        series_list: List[SECFiscalSeries] = []

        for (cik, concept, unit, mode, as_of, snap_time, snap_hash), res_list in clusters.items():
            if not concept or not unit:
                continue

            points: List[SECFiscalSeriesPoint] = []
            conflicts: List[SECFiscalSeriesConflict] = []
            failed_results: List[SECWinnerResolutionResult] = []
            diagnostics: List[str] = []

            # Group selected points by economic period interval to detect duplicates/conflicts
            period_point_map: Dict[Tuple[SECEconomicPeriodKind, Optional[date], date], List[SECFiscalSeriesPoint]] = {}

            for res in res_list:
                if res.status != SECWinnerStatus.SELECTED or not res.selected_candidate or res.selected_value is None:
                    failed_results.append(res)
                    diagnostics.append(f"Excluded non-selected result for group {res.economic_group_key}: {res.status.value}")
                    continue

                cand = res.selected_candidate
                # Guard against missing economic_end_date: NEVER fabricate fallback sentinel dates
                if cand.economic_end_date is None:
                    failed_results.append(res)
                    diagnostics.append(f"Selected winner lacks economic_end_date; excluded from fiscal series (group {res.economic_group_key}).")
                    continue

                point = SECFiscalSeriesPoint(
                    winner_result=res,
                    economic_period_kind=cand.economic_period_kind,
                    start_date=cand.economic_start_date,
                    end_date=cand.economic_end_date,
                    duration_days=cand.duration_days,
                    fiscal_year=cand.fiscal_year,
                    fiscal_period=cand.fiscal_period,
                    selected_value=res.selected_value,
                    selected_accession=res.selected_accession_number,
                    selected_filing_id=res.selected_filing_id,
                    evaluation_snapshot_id=res.evaluation_snapshot_id,
                    evaluation_snapshot_ids=list(res.evaluation_snapshot_ids),
                    evaluation_snapshot_retrieved_at=res.evaluation_snapshot_retrieved_at,
                    evaluation_snapshot_hash=res.evaluation_snapshot_hash,
                    source_concept=res.selected_source_concept,
                    match_strength=cand.match_strength,
                    selection_confidence=res.selection_confidence,
                    is_comparative=cand.is_comparative,
                    is_amendment=cand.is_amendment,
                    diagnostics=list(res.diagnostics),
                )
                p_key = (point.economic_period_kind, point.start_date, point.end_date)
                period_point_map.setdefault(p_key, []).append(point)

            # Deduplicate identical points or record structured conflicts
            for p_key, p_candidates in period_point_map.items():
                if len(p_candidates) == 1:
                    points.append(p_candidates[0])
                else:
                    unique_vals = {p.selected_value for p in p_candidates}
                    if len(unique_vals) == 1:
                        # Identical values: deduplicate deterministically
                        canonical_point = min(
                            p_candidates,
                            key=lambda p: (
                                p.selected_accession or "",
                                str(p.selected_filing_id) if p.selected_filing_id else "",
                                str(p.winner_result.selected_raw_fact_id) if p.winner_result.selected_raw_fact_id else "",
                            )
                        )
                        combined_diag = list(canonical_point.diagnostics)
                        for other in p_candidates:
                            if other != canonical_point:
                                combined_diag.append(f"Corroborated duplicate point from accession {other.selected_accession}.")
                        canonical_point.diagnostics = combined_diag
                        points.append(canonical_point)
                    else:
                        # Differing values: structured series conflict!
                        w_ids = [
                            p.winner_result.selected_candidate.id
                            for p in p_candidates
                            if p.winner_result.selected_candidate
                        ]
                        accs = sorted(list({p.selected_accession for p in p_candidates if p.selected_accession}))
                        conflict_rec = SECFiscalSeriesConflict(
                            economic_period_kind=p_key[0],
                            start_date=p_key[1],
                            end_date=p_key[2],
                            values=sorted(list(unique_vals)),
                            winner_result_ids=w_ids,
                            accession_numbers=accs,
                            reason=f"Conflicting selected values {unique_vals} on identical period [{p_key[1]} to {p_key[2]}].",
                        )
                        conflicts.append(conflict_rec)
                        diagnostics.append(
                            f"Series conflict on period {p_key[0].value} [{p_key[1]} to {p_key[2]}]: conflicting values {unique_vals}."
                        )

            # Sort points by start_date/end_date
            points = sorted(
                points,
                key=lambda p: (
                    p.start_date or p.end_date,
                    p.end_date,
                    p.economic_period_kind.value,
                )
            )

            # Determine series status
            if conflicts:
                series_status = SECFiscalSeriesStatus.CONFLICTED
            elif failed_results:
                series_status = SECFiscalSeriesStatus.PARTIAL
            else:
                series_status = SECFiscalSeriesStatus.VALID

            series_obj = SECFiscalSeries(
                cik=cik,
                canonical_concept=concept,
                unit=unit,
                resolution_mode=mode,
                as_of=as_of,
                evaluation_snapshot_retrieved_at=snap_time,
                evaluation_snapshot_hash=snap_hash,
                status=series_status,
                points=points,
                conflicts=conflicts,
                failed_results=failed_results,
                diagnostics=diagnostics,
            )
            series_list.append(series_obj)

        return series_list


# ─────────────────────────────────────────────────────────────────────────────
# Derivation Eligibility Evaluator
# ─────────────────────────────────────────────────────────────────────────────

class SECFiscalSeriesEvaluator:
    """
    Evaluates quarter derivation eligibility on an assembled SECFiscalSeries.
    Strictly fail-closed; does NOT compute numerical subtraction or derivations in Phase 8B.3A.
    """

    @classmethod
    def _identify_standalone_q2(
        cls,
        series: SECFiscalSeries,
        resolved_fiscal_start: date,
        q1_anchor: Optional[SECFiscalSeriesPoint],
        q2_ytd_anchor: Optional[SECFiscalSeriesPoint],
    ) -> List[SECFiscalSeriesPoint]:
        """
        Identifies standalone Q2 points using strong relational evidence relative to cumulative anchors.
        """
        candidates: List[SECFiscalSeriesPoint] = []

        for p in series.points:
            if p.economic_period_kind != SECEconomicPeriodKind.QUARTER_DURATION:
                continue
            if p.start_date is None or p.start_date <= resolved_fiscal_start:
                continue
            dur = _effective_inclusive_duration(p.start_date, p.end_date, p.duration_days)
            if not _is_valid_quarter_duration(dur):
                continue
            if p.fiscal_period and p.fiscal_period.upper() in ("Q3", "Q4"):
                continue

            # 1. Strong Relational Match with Q2 YTD
            if q2_ytd_anchor is not None:
                if p.end_date == q2_ytd_anchor.end_date:
                    if q1_anchor is None or p.start_date >= q1_anchor.end_date:
                        candidates.append(p)
                        continue

            # 2. Relational Match with Q1 Anchor
            elif q1_anchor is not None:
                if p.start_date >= q1_anchor.end_date and p.end_date <= resolved_fiscal_start + timedelta(days=210):
                    candidates.append(p)
                    continue

            # 3. Fallback: date ordinal proof
            else:
                if (
                    resolved_fiscal_start + timedelta(days=70) <= p.start_date <= resolved_fiscal_start + timedelta(days=125)
                    and p.end_date <= resolved_fiscal_start + timedelta(days=210)
                ):
                    candidates.append(p)

        return candidates

    @classmethod
    def _is_standalone_q2_conflict(
        cls,
        conflict: SECFiscalSeriesConflict,
        resolved_fiscal_start: date,
        q1_anchor: Optional[SECFiscalSeriesPoint],
        q2_ytd_anchor: Optional[SECFiscalSeriesPoint],
    ) -> bool:
        """Determines if a structured conflict lies on the standalone Q2 interval."""
        if conflict.economic_period_kind != SECEconomicPeriodKind.QUARTER_DURATION:
            return False
        if conflict.start_date is None or conflict.start_date <= resolved_fiscal_start:
            return False
        dur = (conflict.end_date - conflict.start_date).days + 1
        if not _is_valid_quarter_duration(dur):
            return False

        if q2_ytd_anchor is not None and conflict.end_date == q2_ytd_anchor.end_date:
            return True
        if q1_anchor is not None and conflict.start_date >= q1_anchor.end_date and conflict.end_date <= resolved_fiscal_start + timedelta(days=210):
            return True
        if (
            resolved_fiscal_start + timedelta(days=70) <= conflict.start_date <= resolved_fiscal_start + timedelta(days=125)
            and conflict.end_date <= resolved_fiscal_start + timedelta(days=210)
        ):
            return True
        return False

    @classmethod
    def _identify_standalone_q3(
        cls,
        series: SECFiscalSeries,
        resolved_fiscal_start: date,
        q2_ytd_anchor: Optional[SECFiscalSeriesPoint],
        q3_ytd_anchor: Optional[SECFiscalSeriesPoint],
    ) -> List[SECFiscalSeriesPoint]:
        """
        Identifies standalone Q3 points using strong relational evidence relative to cumulative anchors.
        """
        candidates: List[SECFiscalSeriesPoint] = []

        for p in series.points:
            if p.economic_period_kind != SECEconomicPeriodKind.QUARTER_DURATION:
                continue
            if p.start_date is None or p.start_date <= resolved_fiscal_start:
                continue
            dur = _effective_inclusive_duration(p.start_date, p.end_date, p.duration_days)
            if not _is_valid_quarter_duration(dur):
                continue
            if p.fiscal_period and p.fiscal_period.upper() in ("Q1", "Q2", "Q4"):
                continue

            # 1. Strong Relational Match with Q3 YTD
            if q3_ytd_anchor is not None:
                if p.end_date == q3_ytd_anchor.end_date:
                    if q2_ytd_anchor is None or p.start_date >= q2_ytd_anchor.end_date:
                        candidates.append(p)
                        continue

            # 2. Relational Match with Q2 YTD Anchor
            elif q2_ytd_anchor is not None:
                if p.start_date >= q2_ytd_anchor.end_date and p.end_date <= resolved_fiscal_start + timedelta(days=300):
                    candidates.append(p)
                    continue

            # 3. Fallback: date ordinal proof
            else:
                if (
                    resolved_fiscal_start + timedelta(days=150) <= p.start_date <= resolved_fiscal_start + timedelta(days=220)
                    and p.end_date <= resolved_fiscal_start + timedelta(days=300)
                ):
                    candidates.append(p)

        return candidates

    @classmethod
    def _is_standalone_q3_conflict(
        cls,
        conflict: SECFiscalSeriesConflict,
        resolved_fiscal_start: date,
        q2_ytd_anchor: Optional[SECFiscalSeriesPoint],
        q3_ytd_anchor: Optional[SECFiscalSeriesPoint],
    ) -> bool:
        """Determines if a structured conflict lies on the standalone Q3 interval."""
        if conflict.economic_period_kind != SECEconomicPeriodKind.QUARTER_DURATION:
            return False
        if conflict.start_date is None or conflict.start_date <= resolved_fiscal_start:
            return False
        dur = (conflict.end_date - conflict.start_date).days + 1
        if not _is_valid_quarter_duration(dur):
            return False

        if q3_ytd_anchor is not None and conflict.end_date == q3_ytd_anchor.end_date:
            return True
        if q2_ytd_anchor is not None and conflict.start_date >= q2_ytd_anchor.end_date and conflict.end_date <= resolved_fiscal_start + timedelta(days=300):
            return True
        if (
            resolved_fiscal_start + timedelta(days=150) <= conflict.start_date <= resolved_fiscal_start + timedelta(days=220)
            and conflict.end_date <= resolved_fiscal_start + timedelta(days=300)
        ):
            return True
        return False

    @classmethod
    def evaluate_quarter_derivation_eligibility(
        cls,
        series: SECFiscalSeries,
        target_quarter: str,
        target_fiscal_year: Optional[int] = None,
        target_fiscal_start_date: Optional[date] = None,
    ) -> SECQuarterDerivationEligibility:
        """
        Determines if a standalone quarter (e.g. Q2, Q3) is eligible for derivation.
        """
        norm_quarter = (target_quarter or "").strip().upper()

        # 1. Instant Concept Check
        concept_def = _CANONICAL_CONCEPTS_MAP.get(series.canonical_concept)
        if concept_def and concept_def.expected_period_type == PeriodType.INSTANT:
            return SECQuarterDerivationEligibility(
                target_quarter=norm_quarter,
                status=SECDerivationEligibilityStatus.CONCEPT_MISMATCH,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                confidence="LOW",
                basis=f"Canonical concept '{series.canonical_concept}' is an INSTANT fact and cannot be derived via duration subtraction.",
                diagnostics=["Instant concepts are ineligible for quarter derivation."],
            )

        # 2. Q4 and TTM are explicitly out of scope
        if norm_quarter in ("Q4", "TTM", "FY"):
            return SECQuarterDerivationEligibility(
                target_quarter=norm_quarter,
                status=SECDerivationEligibilityStatus.UNSUPPORTED_PERIOD,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                confidence="LOW",
                basis=f"Quarter '{norm_quarter}' derivation is explicitly out of scope for Phase 8B.3A.",
                diagnostics=[f"Unsupported derivation period: {norm_quarter}."],
            )

        if norm_quarter not in ("Q1", "Q2", "Q3"):
            return SECQuarterDerivationEligibility(
                target_quarter=norm_quarter,
                status=SECDerivationEligibilityStatus.UNSUPPORTED_PERIOD,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                confidence="LOW",
                basis=f"Unrecognized or unsupported quarter '{norm_quarter}'.",
                diagnostics=[f"Unrecognized target quarter: {norm_quarter}."],
            )

        # 3. Fiscal-Chain Resolution (No arbitrary candidates[0])
        resolved_fiscal_start: Optional[date] = None

        if target_fiscal_start_date is not None:
            resolved_fiscal_start = target_fiscal_start_date
        elif target_fiscal_year is not None:
            matching_starts = _collect_fiscal_start_anchors(series, target_fiscal_year=target_fiscal_year)
            if len(matching_starts) == 1:
                resolved_fiscal_start = next(iter(matching_starts))
            elif len(matching_starts) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter=norm_quarter,
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Multiple distinct economic start dates {matching_starts} found under fiscal_year {target_fiscal_year}.",
                    diagnostics=["Ambiguous fiscal chain: multiple start dates for same fiscal_year."],
                )
            else:
                return SECQuarterDerivationEligibility(
                    target_quarter=norm_quarter,
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"No fiscal chain points found for fiscal_year {target_fiscal_year}.",
                    diagnostics=[f"No points for fiscal_year {target_fiscal_year}."],
                )
        else:
            # Auto-infer candidate fiscal starts strictly from cumulative anchors
            candidate_starts = _collect_fiscal_start_anchors(series, for_quarter=norm_quarter)

            if len(candidate_starts) == 1:
                resolved_fiscal_start = next(iter(candidate_starts))
            elif len(candidate_starts) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter=norm_quarter,
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Multiple fiscal start chains {candidate_starts} exist in series; explicit target_fiscal_start_date or target_fiscal_year required.",
                    diagnostics=["Ambiguous fiscal chain across multiple fiscal years."],
                )
            else:
                return SECQuarterDerivationEligibility(
                    target_quarter=norm_quarter,
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"No fiscal chain found in series for quarter '{norm_quarter}'.",
                    diagnostics=["No candidate fiscal chain found."],
                )

        # 4. Target Quarter Q1 Evaluation
        if norm_quarter == "Q1":
            q1_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and c.start_date == resolved_fiscal_start
                and _is_valid_quarter_duration((c.end_date - c.start_date).days + 1)
            ]
            if q1_conflicts:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q1",
                    status=SECDerivationEligibilityStatus.SERIES_CONFLICT,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Series conflict exists on Q1 period: {q1_conflicts[0].reason}",
                    diagnostics=["Series conflict on Q1 period."],
                )

            q1_points = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and p.start_date == resolved_fiscal_start
                and _is_valid_quarter_duration(_effective_inclusive_duration(p.start_date, p.end_date, p.duration_days))
            ]
            if len(q1_points) == 1:
                q1_p = q1_points[0]
                if q1_p.fiscal_period and q1_p.fiscal_period.upper() in ("Q3", "Q4"):
                    return SECQuarterDerivationEligibility(
                        target_quarter="Q1",
                        status=SECDerivationEligibilityStatus.PERIOD_IDENTITY_UNRESOLVED,
                        canonical_concept=series.canonical_concept,
                        unit=series.unit,
                        confidence="LOW",
                        basis=f"Period identity unresolved: point dates indicate Q1 [{q1_p.start_date} to {q1_p.end_date}] but fiscal_period is {q1_p.fiscal_period}.",
                        diagnostics=["Contradiction between dates and fiscal_period metadata."],
                    )
                return SECQuarterDerivationEligibility(
                    target_quarter="Q1",
                    status=SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=q1_p,
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence=q1_p.selection_confidence,
                    basis="Q1 standalone quarter is already available as a primary anchor; no derivation needed.",
                    diagnostics=["Original standalone Q1 available."],
                )
            elif len(q1_points) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q1",
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Multiple Q1 points found for fiscal start {resolved_fiscal_start}.",
                    diagnostics=["Multiple Q1 points in same fiscal chain."],
                )
            else:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q1",
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Missing Q1 standalone quarter point for fiscal start {resolved_fiscal_start}.",
                    diagnostics=["Q1 operand missing."],
                )

        # 5. Target Quarter Q2 Evaluation
        if norm_quarter == "Q2":
            # Collect available operands for relational verification
            q2_ytd_cands = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and p.start_date == resolved_fiscal_start
                and _is_q2_ytd_duration(_effective_inclusive_duration(p.start_date, p.end_date, p.duration_days))
            ]
            q1_cands = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and p.start_date == resolved_fiscal_start
                and _is_valid_quarter_duration(_effective_inclusive_duration(p.start_date, p.end_date, p.duration_days))
            ]

            q1_anchor = q1_cands[0] if len(q1_cands) == 1 else None
            q2_ytd_anchor = q2_ytd_cands[0] if len(q2_ytd_cands) == 1 else None

            # Check standalone Q2 conflict
            for c in series.conflicts:
                if cls._is_standalone_q2_conflict(c, resolved_fiscal_start, q1_anchor, q2_ytd_anchor):
                    return SECQuarterDerivationEligibility(
                        target_quarter="Q2",
                        status=SECDerivationEligibilityStatus.SERIES_CONFLICT,
                        canonical_concept=series.canonical_concept,
                        unit=series.unit,
                        confidence="LOW",
                        basis=f"Series conflict exists on standalone Q2 period: {c.reason}",
                        diagnostics=["Series conflict on standalone Q2 period."],
                    )

            # Check standalone Q2 points
            q2_standalone_points = cls._identify_standalone_q2(series, resolved_fiscal_start, q1_anchor, q2_ytd_anchor)

            if len(q2_standalone_points) == 1:
                q2_p = q2_standalone_points[0]
                if q2_p.fiscal_period and q2_p.fiscal_period.upper() in ("Q3", "Q4"):
                    return SECQuarterDerivationEligibility(
                        target_quarter="Q2",
                        status=SECDerivationEligibilityStatus.PERIOD_IDENTITY_UNRESOLVED,
                        canonical_concept=series.canonical_concept,
                        unit=series.unit,
                        confidence="LOW",
                        basis=f"Period identity unresolved: point dates indicate Q2 [{q2_p.start_date} to {q2_p.end_date}] but fiscal_period is {q2_p.fiscal_period}.",
                        diagnostics=["Contradiction between dates and fiscal_period metadata."],
                    )
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=q2_p,
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence=q2_p.selection_confidence,
                    basis="Original standalone Q2 quarter point is already available in the series.",
                    diagnostics=["Original standalone Q2 available."],
                )
            elif len(q2_standalone_points) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Multiple standalone Q2 points found for fiscal start {resolved_fiscal_start}.",
                    diagnostics=["Multiple standalone Q2 points in same fiscal chain."],
                )

            # Standalone not available: check operand conflicts
            q2_ytd_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and c.start_date == resolved_fiscal_start
                and _is_q2_ytd_duration((c.end_date - c.start_date).days + 1)
            ]
            q1_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and c.start_date == resolved_fiscal_start
                and _is_valid_quarter_duration((c.end_date - c.start_date).days + 1)
            ]

            if q1_conflicts or q2_ytd_conflicts:
                conflict_reason = (q1_conflicts or q2_ytd_conflicts)[0].reason
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.SERIES_CONFLICT,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Series conflict exists on required Q2 derivation operand: {conflict_reason}",
                    diagnostics=["Series conflict on derivation operand."],
                )

            # Cardinality checks for operands
            if len(q2_ytd_cands) > 1 or len(q1_cands) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis="Multiple distinct candidates for Q1 or Q2 YTD found in resolved fiscal chain.",
                    diagnostics=["Operand cardinality violation."],
                )

            if not q2_ytd_cands:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence="LOW",
                    basis=f"Missing Q2 YTD operand for fiscal start {resolved_fiscal_start}.",
                    diagnostics=["Missing Q2 YTD operand."],
                )

            if not q1_cands:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=q2_ytd_cands[0],
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence="LOW",
                    basis=f"Missing Q1 operand for Q2 derivation (fiscal start {resolved_fiscal_start}).",
                    diagnostics=["Missing Q1 operand."],
                )

            left = q2_ytd_cands[0]
            right = q1_cands[0]

            return cls._validate_derivation_pair(
                target_quarter="Q2",
                expected_formula="Q2_YTD - Q1",
                left=left,
                right=right,
                series=series,
            )

        # 6. Target Quarter Q3 Evaluation
        if norm_quarter == "Q3":
            q3_ytd_cands = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and p.start_date == resolved_fiscal_start
                and _is_q3_ytd_duration(_effective_inclusive_duration(p.start_date, p.end_date, p.duration_days))
            ]
            q2_ytd_cands = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and p.start_date == resolved_fiscal_start
                and _is_q2_ytd_duration(_effective_inclusive_duration(p.start_date, p.end_date, p.duration_days))
            ]

            q2_ytd_anchor = q2_ytd_cands[0] if len(q2_ytd_cands) == 1 else None
            q3_ytd_anchor = q3_ytd_cands[0] if len(q3_ytd_cands) == 1 else None

            # Check standalone Q3 conflict
            for c in series.conflicts:
                if cls._is_standalone_q3_conflict(c, resolved_fiscal_start, q2_ytd_anchor, q3_ytd_anchor):
                    return SECQuarterDerivationEligibility(
                        target_quarter="Q3",
                        status=SECDerivationEligibilityStatus.SERIES_CONFLICT,
                        canonical_concept=series.canonical_concept,
                        unit=series.unit,
                        confidence="LOW",
                        basis=f"Series conflict exists on standalone Q3 period: {c.reason}",
                        diagnostics=["Series conflict on standalone Q3 period."],
                    )

            # Check standalone Q3 points
            q3_standalone_points = cls._identify_standalone_q3(series, resolved_fiscal_start, q2_ytd_anchor, q3_ytd_anchor)

            if len(q3_standalone_points) == 1:
                q3_p = q3_standalone_points[0]
                if q3_p.fiscal_period and q3_p.fiscal_period.upper() in ("Q1", "Q2", "Q4"):
                    return SECQuarterDerivationEligibility(
                        target_quarter="Q3",
                        status=SECDerivationEligibilityStatus.PERIOD_IDENTITY_UNRESOLVED,
                        canonical_concept=series.canonical_concept,
                        unit=series.unit,
                        confidence="LOW",
                        basis=f"Period identity unresolved: point dates indicate Q3 [{q3_p.start_date} to {q3_p.end_date}] but fiscal_period is {q3_p.fiscal_period}.",
                        diagnostics=["Contradiction between dates and fiscal_period metadata."],
                    )
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=q3_p,
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence=q3_p.selection_confidence,
                    basis="Original standalone Q3 quarter point is already available in the series.",
                    diagnostics=["Original standalone Q3 available."],
                )
            elif len(q3_standalone_points) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Multiple standalone Q3 points found for fiscal start {resolved_fiscal_start}.",
                    diagnostics=["Multiple standalone Q3 points in same fiscal chain."],
                )

            # Standalone not available: check operand conflicts
            q3_ytd_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and c.start_date == resolved_fiscal_start
                and _is_q3_ytd_duration((c.end_date - c.start_date).days + 1)
            ]
            q2_ytd_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and c.start_date == resolved_fiscal_start
                and _is_q2_ytd_duration((c.end_date - c.start_date).days + 1)
            ]

            if q3_ytd_conflicts or q2_ytd_conflicts:
                conflict_reason = (q3_ytd_conflicts or q2_ytd_conflicts)[0].reason
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.SERIES_CONFLICT,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Series conflict exists on required Q3 derivation operand: {conflict_reason}",
                    diagnostics=["Series conflict on derivation operand."],
                )

            # Cardinality checks for operands
            if len(q3_ytd_cands) > 1 or len(q2_ytd_cands) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis="Multiple distinct candidates for Q2 YTD or Q3 YTD found in resolved fiscal chain.",
                    diagnostics=["Operand cardinality violation."],
                )

            if not q3_ytd_cands:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence="LOW",
                    basis=f"Missing Q3 YTD operand for fiscal start {resolved_fiscal_start}.",
                    diagnostics=["Missing Q3 YTD operand."],
                )

            if not q2_ytd_cands:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=q3_ytd_cands[0],
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence="LOW",
                    basis=f"Missing Q2 YTD operand for Q3 derivation (fiscal start {resolved_fiscal_start}).",
                    diagnostics=["Missing Q2 YTD operand."],
                )

            left = q3_ytd_cands[0]
            right = q2_ytd_cands[0]

            return cls._validate_derivation_pair(
                target_quarter="Q3",
                expected_formula="Q3_YTD - Q2_YTD",
                left=left,
                right=right,
                series=series,
            )

        return SECQuarterDerivationEligibility(
            target_quarter=norm_quarter,
            status=SECDerivationEligibilityStatus.UNAVAILABLE,
            canonical_concept=series.canonical_concept,
            unit=series.unit,
            confidence="LOW",
            basis=f"Derivation for quarter '{norm_quarter}' is unavailable.",
        )

    @classmethod
    def _validate_derivation_pair(
        cls,
        target_quarter: str,
        expected_formula: str,
        left: SECFiscalSeriesPoint,
        right: SECFiscalSeriesPoint,
        series: SECFiscalSeries,
    ) -> SECQuarterDerivationEligibility:
        """
        Validates economic and temporal consistency between two derivation operands.
        """
        diag: List[str] = []

        # 1. Canonical Concept Match
        left_concept = left.winner_result.selected_candidate.canonical_concept if left.winner_result.selected_candidate else ""
        right_concept = right.winner_result.selected_candidate.canonical_concept if right.winner_result.selected_candidate else ""
        if left_concept != right_concept or left_concept != series.canonical_concept:
            return SECQuarterDerivationEligibility(
                target_quarter=target_quarter,
                status=SECDerivationEligibilityStatus.CONCEPT_MISMATCH,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                left_operand=left,
                right_operand=right,
                confidence="LOW",
                basis=f"Concept mismatch: left ({left_concept}) != right ({right_concept}).",
                diagnostics=["Concept mismatch between operands."],
            )

        # 2. Unit Match
        left_unit = left.winner_result.selected_unit or ""
        right_unit = right.winner_result.selected_unit or ""
        if left_unit != right_unit or left_unit != series.unit:
            return SECQuarterDerivationEligibility(
                target_quarter=target_quarter,
                status=SECDerivationEligibilityStatus.UNIT_MISMATCH,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                left_operand=left,
                right_operand=right,
                confidence="LOW",
                basis=f"Unit mismatch: left unit '{left_unit}' != right unit '{right_unit}'.",
                diagnostics=["Unit mismatch between derivation operands."],
            )

        # 3. Snapshot State Match (evaluation_snapshot_hash & retrieved_at)
        if (
            left.evaluation_snapshot_hash != right.evaluation_snapshot_hash
            or left.evaluation_snapshot_retrieved_at != right.evaluation_snapshot_retrieved_at
        ):
            return SECQuarterDerivationEligibility(
                target_quarter=target_quarter,
                status=SECDerivationEligibilityStatus.SNAPSHOT_MISMATCH,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                left_operand=left,
                right_operand=right,
                confidence="LOW",
                basis=f"Snapshot mismatch: left hash ({left.evaluation_snapshot_hash}) != right hash ({right.evaluation_snapshot_hash}).",
                diagnostics=["Derivation operands must originate from identical evaluation snapshot state."],
            )

        # 4. Mode & As-Of Match
        if left.winner_result.mode != right.winner_result.mode:
            return SECQuarterDerivationEligibility(
                target_quarter=target_quarter,
                status=SECDerivationEligibilityStatus.MODE_MISMATCH,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                left_operand=left,
                right_operand=right,
                confidence="LOW",
                basis=f"Resolution mode mismatch: left ({left.winner_result.mode.value}) != right ({right.winner_result.mode.value}).",
                diagnostics=["Resolution mode mismatch."],
            )

        if left.winner_result.mode == SECWinnerResolutionMode.SYSTEM_AS_OF:
            if left.winner_result.as_of != right.winner_result.as_of:
                return SECQuarterDerivationEligibility(
                    target_quarter=target_quarter,
                    status=SECDerivationEligibilityStatus.AS_OF_MISMATCH,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=left,
                    right_operand=right,
                    confidence="LOW",
                    basis=f"SYSTEM_AS_OF timestamp mismatch: left ({left.winner_result.as_of}) != right ({right.winner_result.as_of}).",
                    diagnostics=["SYSTEM_AS_OF timestamp mismatch."],
                )

        # 5. Fiscal Start Date Match (Economic Start Date)
        if left.start_date != right.start_date or left.start_date is None:
            return SECQuarterDerivationEligibility(
                target_quarter=target_quarter,
                status=SECDerivationEligibilityStatus.FISCAL_START_MISMATCH,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                left_operand=left,
                right_operand=right,
                confidence="LOW",
                basis=f"Fiscal start date mismatch: left start ({left.start_date}) != right start ({right.start_date}).",
                diagnostics=["Operands do not share identical fiscal year start date."],
            )

        # 6. Period Sequence Validity (Chronological & Duration progression)
        if right.end_date >= left.end_date:
            return SECQuarterDerivationEligibility(
                target_quarter=target_quarter,
                status=SECDerivationEligibilityStatus.PERIOD_SEQUENCE_INVALID,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                left_operand=left,
                right_operand=right,
                confidence="LOW",
                basis=f"Invalid period sequence: right end ({right.end_date}) >= left end ({left.end_date}).",
                diagnostics=["Right operand must end strictly before left operand."],
            )

        left_dur = _effective_inclusive_duration(left.start_date, left.end_date, left.duration_days)
        right_dur = _effective_inclusive_duration(right.start_date, right.end_date, right.duration_days)

        if left_dur is not None and right_dur is not None:
            if left_dur <= right_dur:
                return SECQuarterDerivationEligibility(
                    target_quarter=target_quarter,
                    status=SECDerivationEligibilityStatus.PERIOD_SEQUENCE_INVALID,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=left,
                    right_operand=right,
                    confidence="LOW",
                    basis=f"Invalid duration progression: left duration ({left_dur}d) <= right duration ({right_dur}d).",
                    diagnostics=["Left operand duration must exceed right operand duration."],
                )

        # 7. Semantic Quality Policy
        left_ms = (left.match_strength or "").lower()
        right_ms = (right.match_strength or "").lower()

        if left_ms == "legacy_compatible" or right_ms == "legacy_compatible":
            return SECQuarterDerivationEligibility(
                target_quarter=target_quarter,
                status=SECDerivationEligibilityStatus.SEMANTIC_QUALITY_RISK,
                canonical_concept=series.canonical_concept,
                unit=series.unit,
                left_operand=left,
                right_operand=right,
                confidence="LOW",
                basis=f"Semantic quality risk: cannot safely derive across legacy concept variant (left: {left.match_strength}, right: {right.match_strength}).",
                diagnostics=["Legacy concept variant in derivation chain."],
            )

        # Confidence Propagation
        left_conf = (left.selection_confidence or "LOW").upper()
        right_conf = (right.selection_confidence or "LOW").upper()

        if left_conf == "LOW" or right_conf == "LOW":
            derived_confidence = "LOW"
        elif left_conf == "MEDIUM" or right_conf == "MEDIUM" or left_ms == "compatible" or right_ms == "compatible":
            derived_confidence = "MEDIUM"
            if left_ms == "compatible" or right_ms == "compatible":
                diag.append("Derived confidence set to MEDIUM due to compatible semantic alias operand.")
        else:
            derived_confidence = "HIGH"

        # Lineage diagnostics
        if left.selected_accession and right.selected_accession:
            if left.selected_accession == right.selected_accession:
                diag.append(f"Strong same-filing lineage: both operands disclosed in filing {left.selected_accession}.")
            else:
                diag.append(
                    f"Cross-filing restated lineage: left from {left.selected_accession}, right from {right.selected_accession}."
                )

        basis_str = (
            f"Eligible for {target_quarter} derivation using formula '{expected_formula}' "
            f"({left.start_date} to {left.end_date} minus {right.start_date} to {right.end_date})."
        )

        return SECQuarterDerivationEligibility(
            target_quarter=target_quarter,
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept=series.canonical_concept,
            unit=series.unit,
            left_operand=left,
            right_operand=right,
            expected_formula=expected_formula,
            snapshot_hash=series.evaluation_snapshot_hash,
            snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
            confidence=derived_confidence,
            basis=basis_str,
            diagnostics=diag,
        )
