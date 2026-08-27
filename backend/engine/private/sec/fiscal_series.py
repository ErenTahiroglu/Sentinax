"""
backend/engine/private/sec/fiscal_series.py
=============================================
SEC EDGAR Phase 8B.3A / Phase 8B.3A.5: PIT Fiscal Series Assembly,
Fiscal Chain Identity, Series-Conflict & No-Fabrication Hardening.

Core Invariants:
    - Pure Economic Series Assembly: Groups verified winner results by CIK, canonical concept, unit,
      resolution mode, as_of date, and evaluation snapshot state.
    - No Date Fabrication: Missing economic dates are NEVER replaced with epoch or sentinel dates.
      Selected winner candidates lacking economic_end_date are excluded from normal series points and logged.
    - Structured Series Conflict: Conflicting values for identical economic periods are recorded as structured
      SECFiscalSeriesConflict objects and mark the series status as CONFLICTED.
    - Conflict Cannot Be Bypassed: A target quarter cannot bypass a conflicted standalone or operand interval
      to fall back to YTD derivation or declare missing operands; returns SERIES_CONFLICT.
    - Fiscal Chain Resolution: Disambiguates fiscal chains via explicit target_fiscal_start_date, single unique
      fiscal year start, or fails closed with AMBIGUOUS_FISCAL_CHAIN if multiple chains exist. No arbitrary candidates[0].
    - Interval-Based Quarter Identity: Standalone Q1, Q2, Q3 identities require actual interval proof relative
      to the fiscal start and prior quarter boundaries. fp (fiscal_period) is corroborating helper, never sole authority.
    - Cross-Quarter Misidentification Defense: Standalone Q3 cannot masquerade as Q2; Q2 cannot masquerade as Q3.
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
    SECEconomicPeriodKind,
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
            # Gather all points and conflicts with fiscal_year == target_fiscal_year
            matching_starts = {
                p.start_date for p in series.points
                if p.fiscal_year == target_fiscal_year and p.start_date is not None
            }
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
            # Deduce candidate fiscal start chains relevant to target_quarter
            candidate_starts: Set[date] = set()

            # Check points
            for p in series.points:
                if p.start_date is None:
                    continue
                if norm_quarter == "Q1":
                    if p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION and (p.duration_days is None or 70 <= p.duration_days <= 115):
                        candidate_starts.add(p.start_date)
                elif norm_quarter == "Q2":
                    if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION and (p.duration_days is None or 150 <= p.duration_days <= 215):
                        candidate_starts.add(p.start_date)
                    elif p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION and (p.duration_days is None or 70 <= p.duration_days <= 115):
                        candidate_starts.add(p.start_date)
                elif norm_quarter == "Q3":
                    if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION and (p.duration_days is None or 240 <= p.duration_days <= 305):
                        candidate_starts.add(p.start_date)
                    elif p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION and (p.duration_days is None or 150 <= p.duration_days <= 215):
                        candidate_starts.add(p.start_date)

            # Check conflicts as well so a conflict in one year doesn't hide candidate chains
            for c in series.conflicts:
                if c.start_date is not None:
                    candidate_starts.add(c.start_date)

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
            # Check for conflict on Q1 period
            q1_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and c.start_date == resolved_fiscal_start
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
                and (p.duration_days is None or 70 <= p.duration_days <= 115)
            ]
            if len(q1_points) == 1:
                q1_p = q1_points[0]
                # Check fp contradiction
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
            # Check for conflict on Q2 standalone or Q2 YTD or Q1
            q2_ytd_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and c.start_date == resolved_fiscal_start
                and (150 <= (c.end_date - c.start_date).days <= 215)
            ]
            q1_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and c.start_date == resolved_fiscal_start
            ]
            # Check standalone Q2 conflicts (start > fiscal_start, duration ~90d)
            q2_standalone_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and c.start_date is not None
                and c.start_date > resolved_fiscal_start
                and (70 <= (c.end_date - c.start_date).days <= 115)
                and (c.end_date <= resolved_fiscal_start + timedelta(days=215))
            ]

            if q2_standalone_conflicts:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.SERIES_CONFLICT,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Series conflict exists on standalone Q2 period: {q2_standalone_conflicts[0].reason}",
                    diagnostics=["Series conflict on standalone Q2 period."],
                )

            # Check if authoritative standalone Q2 exists via interval proof
            # Standalone Q2 interval: start_date > resolved_fiscal_start and duration ~90d and end_date <= fiscal_start + 215d
            q2_standalone_points = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and p.start_date is not None
                and p.start_date > resolved_fiscal_start
                and (p.duration_days is None or 70 <= p.duration_days <= 115)
                and (p.end_date <= resolved_fiscal_start + timedelta(days=215))
                and (p.start_date <= resolved_fiscal_start + timedelta(days=125))
            ]

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

            # Standalone not available: check derivation operands
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

            q2_ytd_candidates = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and p.start_date == resolved_fiscal_start
                and (p.duration_days is None or 150 <= p.duration_days <= 215)
            ]
            q1_candidates = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and p.start_date == resolved_fiscal_start
                and (p.duration_days is None or 70 <= p.duration_days <= 115)
            ]

            if not q2_ytd_candidates:
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

            if not q1_candidates:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=q2_ytd_candidates[0],
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence="LOW",
                    basis=f"Missing Q1 operand for Q2 derivation (fiscal start {resolved_fiscal_start}).",
                    diagnostics=["Missing Q1 operand."],
                )

            if len(q2_ytd_candidates) > 1 or len(q1_candidates) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q2",
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis="Multiple distinct candidates for Q1 or Q2 YTD found in resolved fiscal chain.",
                    diagnostics=["Operand cardinality violation."],
                )

            left = q2_ytd_candidates[0]
            right = q1_candidates[0]

            return cls._validate_derivation_pair(
                target_quarter="Q2",
                expected_formula="Q2_YTD - Q1",
                left=left,
                right=right,
                series=series,
            )

        # 6. Target Quarter Q3 Evaluation
        if norm_quarter == "Q3":
            # Check for conflict on standalone Q3, Q3 YTD, or Q2 YTD
            q3_standalone_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and c.start_date is not None
                and c.start_date > resolved_fiscal_start
                and (70 <= (c.end_date - c.start_date).days <= 115)
                and (c.start_date >= resolved_fiscal_start + timedelta(days=150))
                and (c.end_date <= resolved_fiscal_start + timedelta(days=310))
            ]
            q3_ytd_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and c.start_date == resolved_fiscal_start
                and (240 <= (c.end_date - c.start_date).days <= 305)
            ]
            q2_ytd_conflicts = [
                c for c in series.conflicts
                if c.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and c.start_date == resolved_fiscal_start
                and (150 <= (c.end_date - c.start_date).days <= 215)
            ]

            if q3_standalone_conflicts:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.SERIES_CONFLICT,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis=f"Series conflict exists on standalone Q3 period: {q3_standalone_conflicts[0].reason}",
                    diagnostics=["Series conflict on standalone Q3 period."],
                )

            # Standalone Q3 interval check
            q3_standalone_points = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
                and p.start_date is not None
                and p.start_date > resolved_fiscal_start
                and (p.duration_days is None or 70 <= p.duration_days <= 115)
                and (p.start_date >= resolved_fiscal_start + timedelta(days=150))
                and (p.end_date <= resolved_fiscal_start + timedelta(days=310))
            ]

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

            # Standalone not available: check derivation operands
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

            q3_ytd_candidates = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and p.start_date == resolved_fiscal_start
                and (p.duration_days is None or 220 <= p.duration_days <= 320 or (p.fiscal_period and p.fiscal_period.upper() == "Q3"))
            ]
            q2_ytd_candidates = [
                p for p in series.points
                if p.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
                and p.start_date == resolved_fiscal_start
                and (p.duration_days is None or 140 <= p.duration_days <= 215 or (p.fiscal_period and p.fiscal_period.upper() == "Q2"))
                and p not in q3_ytd_candidates
            ]

            if not q3_ytd_candidates:
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

            if not q2_ytd_candidates:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.MISSING_OPERAND,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=q3_ytd_candidates[0],
                    snapshot_hash=series.evaluation_snapshot_hash,
                    snapshot_retrieved_at=series.evaluation_snapshot_retrieved_at,
                    confidence="LOW",
                    basis=f"Missing Q2 YTD operand for Q3 derivation (fiscal start {resolved_fiscal_start}).",
                    diagnostics=["Missing Q2 YTD operand."],
                )

            if len(q3_ytd_candidates) > 1 or len(q2_ytd_candidates) > 1:
                return SECQuarterDerivationEligibility(
                    target_quarter="Q3",
                    status=SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    confidence="LOW",
                    basis="Multiple distinct candidates for Q2 YTD or Q3 YTD found in resolved fiscal chain.",
                    diagnostics=["Operand cardinality violation."],
                )

            left = q3_ytd_candidates[0]
            right = q2_ytd_candidates[0]

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

        if left.duration_days is not None and right.duration_days is not None:
            if left.duration_days <= right.duration_days:
                return SECQuarterDerivationEligibility(
                    target_quarter=target_quarter,
                    status=SECDerivationEligibilityStatus.PERIOD_SEQUENCE_INVALID,
                    canonical_concept=series.canonical_concept,
                    unit=series.unit,
                    left_operand=left,
                    right_operand=right,
                    confidence="LOW",
                    basis=f"Invalid duration progression: left duration ({left.duration_days}d) <= right duration ({right.duration_days}d).",
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
