"""
backend/engine/private/sec/quarter_derivation.py
================================================
SEC EDGAR Phase 8B.3B:
Exact Decimal Standalone Quarter Derivation Engine.

Core Invariants:
    - Pure Mathematical Engine: Accepts only validated SECQuarterDerivationEligibility instances.
      No network, no database writes, no clock dependency, no uuid4-based authority.
    - Exact Decimal Arithmetic: Uses Python decimal.localcontext with sufficient dynamic precision
      to prevent precision loss or rounding. Zero float usage.
    - Supported Targets: Q2 (Q2 YTD - Q1) and Q3 (Q3 YTD - Q2 YTD).
      Q4, TTM, and FY are explicitly UNSUPPORTED_PERIOD.
    - Original-Available Priority: If eligibility is ORIGINAL_AVAILABLE, no subtraction is performed;
      the original SEC-reported fact is preserved (derived_value=None, is_derived=False, is_sec_reported=True).
    - Derived Fact Semantics: Derived results have is_derived=True and is_sec_reported=False.
      Lineages of both operands (accessions, filing IDs, raw fact IDs, concepts) are preserved separately
      without fabricating a single filing identity.
    - Defense-in-Depth Revalidation: Tampered or malformed eligibility objects (concept/unit mismatch,
      mode mismatch, snapshot mismatch, non-finite operands, duration invalidity) fail closed.
    - Centralized Period Bounds: Derived quarter intervals (right.end_date + 1 to left.end_date)
      must strictly satisfy centralized QUARTER_MIN_DAYS <= duration <= QUARTER_MAX_DAYS.
    - Deterministic Derivation Key: Stable SHA-256 derivation_key constructed from economic and provenance fields.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.fiscal_series import (
    SECDerivationEligibilityStatus,
    SECFiscalSeriesPoint,
    SECQuarterDerivationEligibility,
    _effective_inclusive_duration,
    _is_q2_ytd_duration,
    _is_q3_ytd_duration,
    _is_valid_quarter_duration,
)
from backend.engine.private.sec.period_context import (
    QUARTER_MAX_DAYS,
    QUARTER_MIN_DAYS,
    SECEconomicPeriodKind,
)
from backend.engine.private.sec.winner_resolver import (
    SECWinnerResolutionMode,
)


# ─────────────────────────────────────────────────────────────────────────────
# Status Enumeration
# ─────────────────────────────────────────────────────────────────────────────

class SECQuarterDerivationStatus(Enum):
    """
    Detailed deterministic status of standalone quarter derivation.
    """
    DERIVED = "derived"
    ORIGINAL_AVAILABLE = "original_available"
    INELIGIBLE = "ineligible"
    INVALID_ELIGIBILITY = "invalid_eligibility"
    NON_FINITE_OPERAND = "non_finite_operand"
    ARITHMETIC_ERROR = "arithmetic_error"
    UNSUPPORTED_PERIOD = "unsupported_period"


# ─────────────────────────────────────────────────────────────────────────────
# Result Model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SECQuarterDerivationResult:
    """
    Result of a deterministic standalone quarter derivation from SEC-reported facts.
    """
    status: SECQuarterDerivationStatus
    target_quarter: str
    cik: str
    canonical_concept: str
    unit: str
    derived_value: Optional[Decimal] = None
    derived_start_date: Optional[date] = None
    derived_end_date: Optional[date] = None
    derived_duration_days: Optional[int] = None
    economic_period_kind: Optional[SECEconomicPeriodKind] = None
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None
    derivation_method: str = "NONE"  # "SUBTRACTION", "ORIGINAL", "NONE"
    formula: Optional[str] = None
    left_operand: Optional[SECFiscalSeriesPoint] = None
    right_operand: Optional[SECFiscalSeriesPoint] = None
    left_value: Optional[Decimal] = None
    right_value: Optional[Decimal] = None
    left_raw_fact_id: Optional[UUID] = None
    right_raw_fact_id: Optional[UUID] = None
    left_accession: Optional[str] = None
    right_accession: Optional[str] = None
    left_filing_id: Optional[UUID] = None
    right_filing_id: Optional[UUID] = None
    left_source_concept: Optional[str] = None
    right_source_concept: Optional[str] = None
    resolution_mode: Optional[SECWinnerResolutionMode] = None
    as_of: Optional[datetime] = None
    snapshot_hash: Optional[str] = None
    snapshot_retrieved_at: Optional[datetime] = None
    snapshot_ids: List[UUID] = field(default_factory=list)
    confidence: Optional[str] = None  # "HIGH", "MEDIUM", "LOW"
    is_derived: bool = False
    is_sec_reported: bool = False
    derivation_key: str = ""
    basis: str = ""
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """
        Serializes derivation result to JSON-compatible dictionary.
        """
        return {
            "status": self.status.value,
            "target_quarter": self.target_quarter,
            "cik": self.cik,
            "canonical_concept": self.canonical_concept,
            "unit": self.unit,
            "derived_value": str(self.derived_value) if self.derived_value is not None else None,
            "derived_start_date": self.derived_start_date.isoformat() if self.derived_start_date else None,
            "derived_end_date": self.derived_end_date.isoformat() if self.derived_end_date else None,
            "derived_duration_days": self.derived_duration_days,
            "economic_period_kind": self.economic_period_kind.value if self.economic_period_kind else None,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "derivation_method": self.derivation_method,
            "formula": self.formula,
            "left_operand": self.left_operand.to_dict() if self.left_operand else None,
            "right_operand": self.right_operand.to_dict() if self.right_operand else None,
            "left_value": str(self.left_value) if self.left_value is not None else None,
            "right_value": str(self.right_value) if self.right_value is not None else None,
            "left_raw_fact_id": str(self.left_raw_fact_id) if self.left_raw_fact_id else None,
            "right_raw_fact_id": str(self.right_raw_fact_id) if self.right_raw_fact_id else None,
            "left_accession": self.left_accession,
            "right_accession": self.right_accession,
            "left_filing_id": str(self.left_filing_id) if self.left_filing_id else None,
            "right_filing_id": str(self.right_filing_id) if self.right_filing_id else None,
            "left_source_concept": self.left_source_concept,
            "right_source_concept": self.right_source_concept,
            "resolution_mode": self.resolution_mode.value if self.resolution_mode else None,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_retrieved_at": self.snapshot_retrieved_at.isoformat() if self.snapshot_retrieved_at else None,
            "snapshot_ids": [str(sid) for sid in self.snapshot_ids],
            "confidence": self.confidence,
            "is_derived": self.is_derived,
            "is_sec_reported": self.is_sec_reported,
            "derivation_key": self.derivation_key,
            "basis": self.basis,
            "diagnostics": self.diagnostics,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions: Exact Decimal Subtraction & Derivation Key
# ─────────────────────────────────────────────────────────────────────────────

def _subtract_decimal_exact(left: Decimal, right: Decimal) -> Decimal:
    """
    Performs lossless, exact Decimal subtraction under an isolated local Decimal context.
    Does NOT mutate the caller's global Decimal context.
    Rejects non-finite values (NaN, Infinity).
    """
    if not isinstance(left, Decimal) or not isinstance(right, Decimal):
        raise TypeError("Both operands must be Decimal instances.")
    if not left.is_finite() or not right.is_finite():
        raise ValueError("Operands must be finite Decimal instances.")

    # Calculate required precision to avoid any precision loss or rounding
    left_t = left.as_tuple()
    right_t = right.as_tuple()
    left_digits = len(left_t.digits) + abs(left_t.exponent)
    right_digits = len(right_t.digits) + abs(right_t.exponent)
    req_prec = max(left_digits, right_digits) + 50
    prec = max(100, req_prec)

    with localcontext() as ctx:
        ctx.prec = prec
        ctx.traps[Inexact] = False
        ctx.traps[Rounded] = False
        result = left - right

    return result


def _compute_derivation_key(
    cik: str,
    canonical_concept: str,
    unit: str,
    target_quarter: str,
    start_date: Optional[date],
    end_date: Optional[date],
    resolution_mode: Optional[SECWinnerResolutionMode],
    as_of: Optional[datetime],
    snapshot_hash: Optional[str],
    left_raw_fact_id: Optional[UUID],
    right_raw_fact_id: Optional[UUID],
    status: SECQuarterDerivationStatus,
) -> str:
    """
    Constructs a deterministic, collision-resistant SHA-256 derivation key.
    """
    parts = [
        normalize_cik(cik) if cik else "",
        canonical_concept or "",
        unit or "",
        target_quarter or "",
        start_date.isoformat() if start_date else "none",
        end_date.isoformat() if end_date else "none",
        resolution_mode.value if resolution_mode else "none",
        as_of.isoformat() if as_of else "none",
        snapshot_hash or "none",
        str(left_raw_fact_id) if left_raw_fact_id else "none",
        str(right_raw_fact_id) if right_raw_fact_id else "none",
        status.value,
    ]
    raw_key = ":".join(parts)
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Pure Standalone Quarter Deriver
# ─────────────────────────────────────────────────────────────────────────────

class SECQuarterDeriver:
    """
    Pure deterministic engine for deriving standalone quarters (Q2, Q3)
    from verified SECQuarterDerivationEligibility contracts.
    """

    @classmethod
    def derive_quarter(
        cls,
        eligibility: SECQuarterDerivationEligibility,
    ) -> SECQuarterDerivationResult:
        """
        Main entrypoint: evaluates eligibility contract and performs exact arithmetic if ELIGIBLE.
        """
        norm_quarter = (eligibility.target_quarter or "").strip().upper()

        # 1. Unsupported Period Check (Q4, TTM, FY or unrecognized)
        if norm_quarter in ("Q4", "TTM", "FY"):
            derivation_key = _compute_derivation_key(
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=None,
                as_of=None,
                snapshot_hash=eligibility.snapshot_hash,
                left_raw_fact_id=None,
                right_raw_fact_id=None,
                status=SECQuarterDerivationStatus.UNSUPPORTED_PERIOD,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.UNSUPPORTED_PERIOD,
                target_quarter=norm_quarter,
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis=f"Quarter '{norm_quarter}' derivation is explicitly out of scope / unsupported.",
                diagnostics=[f"Unsupported derivation period: {norm_quarter}."],
            )

        # 2. Original Available Priority: No subtraction performed
        if eligibility.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE:
            left = eligibility.left_operand
            if left is None:
                derivation_key = _compute_derivation_key(
                    cik="",
                    canonical_concept=eligibility.canonical_concept,
                    unit=eligibility.unit,
                    target_quarter=norm_quarter,
                    start_date=None,
                    end_date=None,
                    resolution_mode=None,
                    as_of=None,
                    snapshot_hash=eligibility.snapshot_hash,
                    left_raw_fact_id=None,
                    right_raw_fact_id=None,
                    status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                )
                return SECQuarterDerivationResult(
                    status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                    target_quarter=norm_quarter,
                    cik="",
                    canonical_concept=eligibility.canonical_concept,
                    unit=eligibility.unit,
                    derivation_method="NONE",
                    confidence="LOW",
                    is_derived=False,
                    is_sec_reported=False,
                    derivation_key=derivation_key,
                    basis="ORIGINAL_AVAILABLE eligibility missing original fact point in left_operand.",
                    diagnostics=["Missing original fact operand."],
                )

            # Check value finiteness
            if not isinstance(left.selected_value, Decimal) or not left.selected_value.is_finite():
                derivation_key = _compute_derivation_key(
                    cik=left.winner_result.cik,
                    canonical_concept=eligibility.canonical_concept,
                    unit=eligibility.unit,
                    target_quarter=norm_quarter,
                    start_date=left.start_date,
                    end_date=left.end_date,
                    resolution_mode=left.winner_result.mode,
                    as_of=left.winner_result.as_of,
                    snapshot_hash=left.evaluation_snapshot_hash,
                    left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                    right_raw_fact_id=None,
                    status=SECQuarterDerivationStatus.NON_FINITE_OPERAND,
                )
                return SECQuarterDerivationResult(
                    status=SECQuarterDerivationStatus.NON_FINITE_OPERAND,
                    target_quarter=norm_quarter,
                    cik=left.winner_result.cik,
                    canonical_concept=eligibility.canonical_concept,
                    unit=eligibility.unit,
                    derivation_method="NONE",
                    confidence="LOW",
                    is_derived=False,
                    is_sec_reported=False,
                    derivation_key=derivation_key,
                    basis="Original SEC-reported fact contains non-finite or invalid Decimal value.",
                    diagnostics=["Non-finite Decimal in original fact."],
                )

            cik = left.winner_result.cik
            mode = left.winner_result.mode
            as_of = left.winner_result.as_of
            snap_hash = eligibility.snapshot_hash or left.evaluation_snapshot_hash
            snap_time = eligibility.snapshot_retrieved_at or left.evaluation_snapshot_retrieved_at
            snap_ids = list(left.evaluation_snapshot_ids)
            conf = eligibility.confidence or left.selection_confidence or "HIGH"

            derivation_key = _compute_derivation_key(
                cik=cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=left.start_date,
                end_date=left.end_date,
                resolution_mode=mode,
                as_of=as_of,
                snapshot_hash=snap_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=None,
                status=SECQuarterDerivationStatus.ORIGINAL_AVAILABLE,
            )

            diag = list(eligibility.diagnostics)
            diag.append("Original SEC-reported fact preserved; no synthetic derivation performed.")

            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.ORIGINAL_AVAILABLE,
                target_quarter=norm_quarter,
                cik=cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derived_value=None,  # Do not synthesize a duplicate derived number
                derived_start_date=left.start_date,
                derived_end_date=left.end_date,
                derived_duration_days=left.duration_days,
                economic_period_kind=left.economic_period_kind,
                fiscal_year=left.fiscal_year,
                fiscal_period=left.fiscal_period or norm_quarter,
                derivation_method="ORIGINAL",
                formula=None,
                left_operand=left,
                right_operand=None,
                left_value=left.selected_value,
                right_value=None,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=None,
                left_accession=left.selected_accession,
                right_accession=None,
                left_filing_id=left.selected_filing_id,
                right_filing_id=None,
                left_source_concept=left.source_concept,
                right_source_concept=None,
                resolution_mode=mode,
                as_of=as_of,
                snapshot_hash=snap_hash,
                snapshot_retrieved_at=snap_time,
                snapshot_ids=snap_ids,
                confidence=conf,
                is_derived=False,
                is_sec_reported=True,
                derivation_key=derivation_key,
                basis="Original standalone quarter point is already available as an SEC-reported fact.",
                diagnostics=diag,
            )

        # 3. Non-Eligible Status Check
        if eligibility.status != SECDerivationEligibilityStatus.ELIGIBLE:
            derivation_key = _compute_derivation_key(
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=None,
                as_of=None,
                snapshot_hash=eligibility.snapshot_hash,
                left_raw_fact_id=None,
                right_raw_fact_id=None,
                status=SECQuarterDerivationStatus.INELIGIBLE,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INELIGIBLE,
                target_quarter=norm_quarter,
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence=eligibility.confidence or "LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis=f"Ineligible for quarter derivation: {eligibility.status.value}. {eligibility.basis}",
                diagnostics=list(eligibility.diagnostics),
            )

        # 4. ELIGIBLE Path: Strict Defense-in-Depth Contract Revalidation
        if norm_quarter not in ("Q2", "Q3"):
            derivation_key = _compute_derivation_key(
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=None,
                as_of=None,
                snapshot_hash=eligibility.snapshot_hash,
                left_raw_fact_id=None,
                right_raw_fact_id=None,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis=f"Derivation is only supported for Q2 and Q3, but received '{norm_quarter}'.",
                diagnostics=["Unsupported quarter in ELIGIBLE contract."],
            )

        left = eligibility.left_operand
        right = eligibility.right_operand

        if left is None or right is None:
            derivation_key = _compute_derivation_key(
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=None,
                as_of=None,
                snapshot_hash=eligibility.snapshot_hash,
                left_raw_fact_id=None,
                right_raw_fact_id=None,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik="",
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis="ELIGIBLE derivation contract is missing left or right operand.",
                diagnostics=["Missing operand in ELIGIBLE contract."],
            )

        # Finiteness & Decimal Type Validation
        if not isinstance(left.selected_value, Decimal) or not isinstance(right.selected_value, Decimal):
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis="Operands selected_value must be Decimal instances.",
                diagnostics=["Non-Decimal operand value."],
            )

        if not left.selected_value.is_finite() or not right.selected_value.is_finite():
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.NON_FINITE_OPERAND,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.NON_FINITE_OPERAND,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis="Operand contains non-finite Decimal value (NaN or Infinity).",
                diagnostics=["Non-finite Decimal operand."],
            )

        # Concept & Unit Matching
        left_concept = left.winner_result.selected_candidate.canonical_concept if left.winner_result.selected_candidate else ""
        right_concept = right.winner_result.selected_candidate.canonical_concept if right.winner_result.selected_candidate else ""
        if left_concept != right_concept or left_concept != eligibility.canonical_concept:
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis=f"Concept mismatch: left ({left_concept}) != right ({right_concept}) or != contract ({eligibility.canonical_concept}).",
                diagnostics=["Canonical concept mismatch."],
            )

        left_unit = left.winner_result.selected_unit or ""
        right_unit = right.winner_result.selected_unit or ""
        if left_unit != right_unit or left_unit != eligibility.unit:
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis=f"Unit mismatch: left ({left_unit}) != right ({right_unit}) or != contract ({eligibility.unit}).",
                diagnostics=["Unit mismatch between operands."],
            )

        # Snapshot State & Resolution Mode Consistency
        if (
            left.evaluation_snapshot_hash != right.evaluation_snapshot_hash
            or left.evaluation_snapshot_retrieved_at != right.evaluation_snapshot_retrieved_at
            or (eligibility.snapshot_hash and left.evaluation_snapshot_hash != eligibility.snapshot_hash)
            or (eligibility.snapshot_retrieved_at and left.evaluation_snapshot_retrieved_at != eligibility.snapshot_retrieved_at)
        ):
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis="Snapshot state mismatch between operands and eligibility contract.",
                diagnostics=["Snapshot state mismatch."],
            )

        if left.winner_result.mode != right.winner_result.mode:
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis="Resolution mode mismatch between operands.",
                diagnostics=["Resolution mode mismatch."],
            )

        if left.winner_result.mode == SECWinnerResolutionMode.SYSTEM_AS_OF:
            if left.winner_result.as_of != right.winner_result.as_of:
                derivation_key = _compute_derivation_key(
                    cik=left.winner_result.cik,
                    canonical_concept=eligibility.canonical_concept,
                    unit=eligibility.unit,
                    target_quarter=norm_quarter,
                    start_date=None,
                    end_date=None,
                    resolution_mode=left.winner_result.mode,
                    as_of=left.winner_result.as_of,
                    snapshot_hash=left.evaluation_snapshot_hash,
                    left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                    right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                    status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                )
                return SECQuarterDerivationResult(
                    status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                    target_quarter=norm_quarter,
                    cik=left.winner_result.cik,
                    canonical_concept=eligibility.canonical_concept,
                    unit=eligibility.unit,
                    derivation_method="NONE",
                    confidence="LOW",
                    is_derived=False,
                    is_sec_reported=False,
                    derivation_key=derivation_key,
                    basis="SYSTEM_AS_OF as_of timestamp mismatch between operands.",
                    diagnostics=["SYSTEM_AS_OF as_of timestamp mismatch."],
                )

        # Fiscal Start & Period Sequence
        if left.start_date != right.start_date or left.start_date is None:
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis="Operands do not share identical fiscal start date.",
                diagnostics=["Fiscal start date mismatch."],
            )

        if right.end_date >= left.end_date:
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=None,
                end_date=None,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="NONE",
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis=f"Period sequence invalid: right operand end date ({right.end_date}) >= left operand end date ({left.end_date}).",
                diagnostics=["Right operand must end strictly before left operand."],
            )

        # Target-Specific Structural Checks
        left_dur = _effective_inclusive_duration(left.start_date, left.end_date, left.duration_days)
        right_dur = _effective_inclusive_duration(right.start_date, right.end_date, right.duration_days)

        if norm_quarter == "Q2":
            if eligibility.expected_formula != "Q2_YTD - Q1":
                return cls._make_invalid_eligibility(eligibility, left, right, "Invalid formula for Q2 derivation.")
            if left.economic_period_kind != SECEconomicPeriodKind.YTD_DURATION:
                return cls._make_invalid_eligibility(eligibility, left, right, "Left operand for Q2 must be YTD_DURATION.")
            if right.economic_period_kind != SECEconomicPeriodKind.QUARTER_DURATION:
                return cls._make_invalid_eligibility(eligibility, left, right, "Right operand for Q2 must be QUARTER_DURATION.")
            if left_dur is None or not _is_q2_ytd_duration(left_dur):
                return cls._make_invalid_eligibility(eligibility, left, right, f"Left operand duration ({left_dur}d) is not a valid 6M YTD duration.")
            if right_dur is None or not _is_valid_quarter_duration(right_dur):
                return cls._make_invalid_eligibility(eligibility, left, right, f"Right operand duration ({right_dur}d) is not a valid quarter duration.")
            if left_dur <= right_dur:
                return cls._make_invalid_eligibility(eligibility, left, right, "Q2 YTD duration must exceed Q1 duration.")

        elif norm_quarter == "Q3":
            if eligibility.expected_formula != "Q3_YTD - Q2_YTD":
                return cls._make_invalid_eligibility(eligibility, left, right, "Invalid formula for Q3 derivation.")
            if left.economic_period_kind != SECEconomicPeriodKind.YTD_DURATION:
                return cls._make_invalid_eligibility(eligibility, left, right, "Left operand for Q3 must be YTD_DURATION.")
            if right.economic_period_kind != SECEconomicPeriodKind.YTD_DURATION:
                return cls._make_invalid_eligibility(eligibility, left, right, "Right operand for Q3 must be YTD_DURATION.")
            if left_dur is None or not _is_q3_ytd_duration(left_dur):
                return cls._make_invalid_eligibility(eligibility, left, right, f"Left operand duration ({left_dur}d) is not a valid 9M YTD duration.")
            if right_dur is None or not _is_q2_ytd_duration(right_dur):
                return cls._make_invalid_eligibility(eligibility, left, right, f"Right operand duration ({right_dur}d) is not a valid 6M YTD duration.")
            if left_dur <= right_dur:
                return cls._make_invalid_eligibility(eligibility, left, right, "Q3 YTD duration must exceed Q2 YTD duration.")

        # Derived Period Dates Calculation & Bounds Check
        derived_start_date = right.end_date + timedelta(days=1)
        derived_end_date = left.end_date
        derived_duration_days = (derived_end_date - derived_start_date).days + 1

        if derived_start_date > derived_end_date or not _is_valid_quarter_duration(derived_duration_days):
            return cls._make_invalid_eligibility(
                eligibility, left, right,
                f"Derived interval [{derived_start_date} to {derived_end_date}] duration ({derived_duration_days}d) is outside centralized quarter bounds ({QUARTER_MIN_DAYS}-{QUARTER_MAX_DAYS}d)."
            )

        # Fiscal Year Consistency Check
        if left.fiscal_year is not None and right.fiscal_year is not None and left.fiscal_year != right.fiscal_year:
            return cls._make_invalid_eligibility(
                eligibility, left, right,
                f"Fiscal year mismatch between operands: left ({left.fiscal_year}) != right ({right.fiscal_year})."
            )

        derived_fy = left.fiscal_year if left.fiscal_year is not None else right.fiscal_year
        derived_fp = norm_quarter

        # Perform Exact Lossless Decimal Subtraction
        try:
            derived_val = _subtract_decimal_exact(left.selected_value, right.selected_value)
        except Exception as exc:
            derivation_key = _compute_derivation_key(
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                target_quarter=norm_quarter,
                start_date=derived_start_date,
                end_date=derived_end_date,
                resolution_mode=left.winner_result.mode,
                as_of=left.winner_result.as_of,
                snapshot_hash=left.evaluation_snapshot_hash,
                left_raw_fact_id=left.winner_result.selected_raw_fact_id,
                right_raw_fact_id=right.winner_result.selected_raw_fact_id,
                status=SECQuarterDerivationStatus.ARITHMETIC_ERROR,
            )
            return SECQuarterDerivationResult(
                status=SECQuarterDerivationStatus.ARITHMETIC_ERROR,
                target_quarter=norm_quarter,
                cik=left.winner_result.cik,
                canonical_concept=eligibility.canonical_concept,
                unit=eligibility.unit,
                derivation_method="SUBTRACTION",
                formula=eligibility.expected_formula,
                left_operand=left,
                right_operand=right,
                left_value=left.selected_value,
                right_value=right.selected_value,
                confidence="LOW",
                is_derived=False,
                is_sec_reported=False,
                derivation_key=derivation_key,
                basis=f"Arithmetic error during exact Decimal subtraction: {exc}",
                diagnostics=[f"Arithmetic error: {exc}"],
            )

        # Provenance Metadata & Lineage
        cik = left.winner_result.cik
        mode = left.winner_result.mode
        as_of = left.winner_result.as_of
        snap_hash = left.evaluation_snapshot_hash
        snap_time = left.evaluation_snapshot_retrieved_at
        all_sids = list(left.evaluation_snapshot_ids) + list(right.evaluation_snapshot_ids)
        combined_snap_ids = sorted(list({sid for sid in all_sids if sid is not None}), key=str)

        diag = list(eligibility.diagnostics)
        if left.selected_accession and right.selected_accession:
            if left.selected_accession == right.selected_accession:
                diag.append(f"Derived from same-filing operand lineage ({left.selected_accession}).")
            else:
                diag.append(
                    f"Derived from cross-filing operand lineage (left: {left.selected_accession}, right: {right.selected_accession})."
                )

        if left.fiscal_year is None and right.fiscal_year is not None:
            diag.append(f"Inferred derived fiscal_year {right.fiscal_year} from right operand.")
        elif right.fiscal_year is None and left.fiscal_year is not None:
            diag.append(f"Inferred derived fiscal_year {left.fiscal_year} from left operand.")

        basis_str = (
            f"Sentinax-derived standalone {norm_quarter} from SEC-reported cumulative facts: "
            f"{left.selected_value} - {right.selected_value} = {derived_val}."
        )

        derivation_key = _compute_derivation_key(
            cik=cik,
            canonical_concept=eligibility.canonical_concept,
            unit=eligibility.unit,
            target_quarter=norm_quarter,
            start_date=derived_start_date,
            end_date=derived_end_date,
            resolution_mode=mode,
            as_of=as_of,
            snapshot_hash=snap_hash,
            left_raw_fact_id=left.winner_result.selected_raw_fact_id,
            right_raw_fact_id=right.winner_result.selected_raw_fact_id,
            status=SECQuarterDerivationStatus.DERIVED,
        )

        return SECQuarterDerivationResult(
            status=SECQuarterDerivationStatus.DERIVED,
            target_quarter=norm_quarter,
            cik=cik,
            canonical_concept=eligibility.canonical_concept,
            unit=eligibility.unit,
            derived_value=derived_val,
            derived_start_date=derived_start_date,
            derived_end_date=derived_end_date,
            derived_duration_days=derived_duration_days,
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_year=derived_fy,
            fiscal_period=derived_fp,
            derivation_method="SUBTRACTION",
            formula=eligibility.expected_formula,
            left_operand=left,
            right_operand=right,
            left_value=left.selected_value,
            right_value=right.selected_value,
            left_raw_fact_id=left.winner_result.selected_raw_fact_id,
            right_raw_fact_id=right.winner_result.selected_raw_fact_id,
            left_accession=left.selected_accession,
            right_accession=right.selected_accession,
            left_filing_id=left.selected_filing_id,
            right_filing_id=right.selected_filing_id,
            left_source_concept=left.source_concept,
            right_source_concept=right.source_concept,
            resolution_mode=mode,
            as_of=as_of,
            snapshot_hash=snap_hash,
            snapshot_retrieved_at=snap_time,
            snapshot_ids=combined_snap_ids,
            confidence=eligibility.confidence or "LOW",
            is_derived=True,
            is_sec_reported=False,
            derivation_key=derivation_key,
            basis=basis_str,
            diagnostics=diag,
        )

    @classmethod
    def _make_invalid_eligibility(
        cls,
        eligibility: SECQuarterDerivationEligibility,
        left: Optional[SECFiscalSeriesPoint],
        right: Optional[SECFiscalSeriesPoint],
        reason: str,
    ) -> SECQuarterDerivationResult:
        """
        Constructs an INVALID_ELIGIBILITY result when contract revalidation fails.
        """
        cik = left.winner_result.cik if left else ""
        mode = left.winner_result.mode if left else None
        as_of = left.winner_result.as_of if left else None
        snap_hash = left.evaluation_snapshot_hash if left else eligibility.snapshot_hash
        left_rf = left.winner_result.selected_raw_fact_id if left else None
        right_rf = right.winner_result.selected_raw_fact_id if right else None

        derivation_key = _compute_derivation_key(
            cik=cik,
            canonical_concept=eligibility.canonical_concept,
            unit=eligibility.unit,
            target_quarter=eligibility.target_quarter,
            start_date=None,
            end_date=None,
            resolution_mode=mode,
            as_of=as_of,
            snapshot_hash=snap_hash,
            left_raw_fact_id=left_rf,
            right_raw_fact_id=right_rf,
            status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
        )

        return SECQuarterDerivationResult(
            status=SECQuarterDerivationStatus.INVALID_ELIGIBILITY,
            target_quarter=eligibility.target_quarter,
            cik=cik,
            canonical_concept=eligibility.canonical_concept,
            unit=eligibility.unit,
            derivation_method="NONE",
            formula=eligibility.expected_formula,
            left_operand=left,
            right_operand=right,
            left_value=left.selected_value if left else None,
            right_value=right.selected_value if right else None,
            left_raw_fact_id=left_rf,
            right_raw_fact_id=right_rf,
            left_accession=left.selected_accession if left else None,
            right_accession=right.selected_accession if right else None,
            left_filing_id=left.selected_filing_id if left else None,
            right_filing_id=right.selected_filing_id if right else None,
            left_source_concept=left.source_concept if left else None,
            right_source_concept=right.source_concept if right else None,
            resolution_mode=mode,
            as_of=as_of,
            snapshot_hash=snap_hash,
            confidence="LOW",
            is_derived=False,
            is_sec_reported=False,
            derivation_key=derivation_key,
            basis=f"Invalid derivation eligibility contract: {reason}",
            diagnostics=[reason],
        )
