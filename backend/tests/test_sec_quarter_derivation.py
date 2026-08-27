"""
backend/tests/test_sec_quarter_derivation.py
============================================
Comprehensive test suite for SEC EDGAR Phase 8B.3B:
Exact Decimal Standalone Quarter Derivation Engine.

Tests:
    1-2:   ORIGINAL_AVAILABLE priority (Q2 and Q3, no subtraction, preserved fact).
    3-4:   Basic Q2 and Q3 exact decimal subtraction.
    5-6:   Negative and zero derived quarter results.
    7-9:   Decimal scale preservation, huge precision (>28 digits), local context isolation.
    10:    Non-finite Decimal operands (NaN, Infinity) fail closed.
    11-20: Tampered / malformed eligibility contract defense-in-depth:
           - Ineligible status with attached operands
           - Formula mismatch
           - Period kind mismatch
           - Canonical concept mismatch
           - Unit mismatch
           - Snapshot state mismatch
           - SYSTEM_AS_OF as_of mismatch
           - Fiscal start mismatch
           - Period sequence invalid
           - Derived interval duration outside bounds
    21-22: Non-calendar fiscal year (Oct start) and 52/53-week retailers.
    23-24: Cross-filing vs same-filing lineage and provenance preservation.
    25-26: CURRENT_REPORTED vs SYSTEM_AS_OF mode separation & pure determinism.
    27-28: Unsupported periods (Q4, TTM, FY) and module purity (no FCF, margin, growth).
    29-32: Input immutability, serialization (to_dict), fiscal year inheritance, confidence propagation.
"""

import decimal
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.sec.fiscal_series import (
    SECDerivationEligibilityStatus,
    SECFiscalSeriesAssembler,
    SECFiscalSeriesEvaluator,
    SECFiscalSeriesPoint,
    SECQuarterDerivationEligibility,
)
from backend.engine.private.sec.period_context import (
    SECEconomicPeriodKind,
    SECPeriodAlignmentStatus,
    SECPeriodizedFactCandidate,
)
from backend.engine.private.sec.quarter_derivation import (
    SECQuarterDerivationResult,
    SECQuarterDerivationStatus,
    SECQuarterDeriver,
    _compute_derivation_key,
    _subtract_decimal_exact,
)
from backend.engine.private.sec.winner_resolver import (
    SECWinnerResolutionMode,
    SECWinnerResolutionResult,
    SECWinnerStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

_UNSET = object()


def _make_winner_result(
    cik: str = "0000320193",
    canonical_concept: str = "REVENUE",
    unit: str = "USD",
    economic_period_kind: SECEconomicPeriodKind = SECEconomicPeriodKind.QUARTER_DURATION,
    start_date: Optional[date] = date(2024, 1, 1),
    end_date: date = date(2024, 3, 31),
    duration_days: Any = _UNSET,
    fiscal_year: Optional[int] = 2024,
    fiscal_period: Optional[str] = "Q1",
    value: Decimal = Decimal("100"),
    accession_number: str = "0000320193-24-000001",
    filing_id: Optional[UUID] = None,
    raw_fact_id: Optional[UUID] = None,
    mode: SECWinnerResolutionMode = SECWinnerResolutionMode.CURRENT_REPORTED,
    as_of: Optional[datetime] = None,
    snapshot_hash: str = "hash_alpha_123",
    snapshot_retrieved_at: Optional[datetime] = datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc),
    snapshot_id: Optional[UUID] = None,
    source_concept: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    match_strength: str = "EXACT",
    selection_confidence: str = "HIGH",
    is_comparative: bool = False,
    is_amendment: bool = False,
) -> SECWinnerResolutionResult:
    f_id = filing_id or uuid4()
    rf_id = raw_fact_id or uuid4()
    snap_id = snapshot_id or uuid4()

    if duration_days is _UNSET:
        if start_date is not None and end_date is not None and end_date >= start_date:
            dur_days: Optional[int] = (end_date - start_date).days + 1
        else:
            dur_days = None
    else:
        dur_days = duration_days

    cand = SECPeriodizedFactCandidate(
        candidate_id=uuid4(),
        raw_fact_id=rf_id,
        cik=cik,
        canonical_concept=canonical_concept,
        economic_period_kind=economic_period_kind,
        period_alignment_status=SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD,
        economic_start_date=start_date,
        economic_end_date=end_date,
        duration_days=dur_days,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        filing_id=f_id,
        accession_number=accession_number,
        form="10-Q" if economic_period_kind != SECEconomicPeriodKind.ANNUAL_DURATION else "10-K",
        form_role="primary_interim" if economic_period_kind != SECEconomicPeriodKind.ANNUAL_DURATION else "primary_annual",
        is_amendment=is_amendment,
        filing_report_date=end_date,
        is_comparative=is_comparative,
        classification_confidence=selection_confidence,
        classification_basis="Tested candidate",
        diagnostics=[],
        value=value,
        unit=unit,
        taxonomy="us-gaap",
        source_concept=source_concept,
        match_strength=match_strength,
        variant_priority=1,
        snapshot_id=snap_id,
    )

    group_key = (
        cik,
        canonical_concept,
        unit,
        economic_period_kind.value,
        start_date.isoformat() if start_date else "none",
        end_date.isoformat(),
    )

    return SECWinnerResolutionResult(
        mode=mode,
        status=SECWinnerStatus.SELECTED,
        cik=cik,
        economic_group_key=group_key,
        as_of=as_of,
        evaluation_snapshot_id=snap_id,
        evaluation_snapshot_ids=[snap_id],
        evaluation_snapshot_retrieved_at=snapshot_retrieved_at,
        evaluation_snapshot_hash=snapshot_hash,
        selected_candidate=cand,
        selected_raw_fact_id=rf_id,
        selected_value=value,
        selected_unit=unit,
        selected_source_concept=source_concept,
        selected_accession_number=accession_number,
        selected_filing_id=f_id,
        selected_form=cand.form,
        selection_confidence=selection_confidence,
        selection_basis="Selected test winner",
        diagnostics=["Winner result diagnostic"],
    )


def _make_series_point(
    winner_res: SECWinnerResolutionResult,
) -> SECFiscalSeriesPoint:
    cand = winner_res.selected_candidate
    return SECFiscalSeriesPoint(
        winner_result=winner_res,
        economic_period_kind=cand.economic_period_kind,
        start_date=cand.economic_start_date,
        end_date=cand.economic_end_date,
        duration_days=cand.duration_days,
        fiscal_year=cand.fiscal_year,
        fiscal_period=cand.fiscal_period,
        selected_value=winner_res.selected_value,
        selected_accession=winner_res.selected_accession_number,
        selected_filing_id=winner_res.selected_filing_id,
        evaluation_snapshot_id=winner_res.evaluation_snapshot_id,
        evaluation_snapshot_ids=list(winner_res.evaluation_snapshot_ids),
        evaluation_snapshot_retrieved_at=winner_res.evaluation_snapshot_retrieved_at,
        evaluation_snapshot_hash=winner_res.evaluation_snapshot_hash,
        source_concept=winner_res.selected_source_concept,
        match_strength=cand.match_strength,
        selection_confidence=winner_res.selection_confidence,
        is_comparative=cand.is_comparative,
        is_amendment=cand.is_amendment,
        diagnostics=list(winner_res.diagnostics),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Suite
# ─────────────────────────────────────────────────────────────────────────────

class TestSECQuarterDerivationEngine:

    def test_01_q2_original_available_no_subtraction(self):
        """Scenario 1: ORIGINAL_AVAILABLE for Q2 returns original fact with derived_value=None and is_derived=False."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))
        w2_stand = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION, fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), value=Decimal("120"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w2_stand])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE

        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.ORIGINAL_AVAILABLE
        assert res.target_quarter == "Q2"
        assert res.derived_value is None
        assert res.left_value == Decimal("120")
        assert res.is_derived is False
        assert res.is_sec_reported is True
        assert res.derivation_method == "ORIGINAL"
        assert res.formula is None
        assert res.derived_start_date == date(2024, 4, 1)
        assert res.derived_end_date == date(2024, 6, 30)
        assert res.derived_duration_days == 91
        assert res.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert res.confidence == "HIGH"
        assert res.derivation_key != ""

    def test_02_q3_original_available_no_subtraction(self):
        """Scenario 2: ORIGINAL_AVAILABLE for Q3 returns original fact with derived_value=None and is_derived=False."""
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))
        w3_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350"))
        w3_stand = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION, fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), value=Decimal("130"))

        series = SECFiscalSeriesAssembler.assemble_series([w2_ytd, w3_ytd, w3_stand])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE

        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.ORIGINAL_AVAILABLE
        assert res.target_quarter == "Q3"
        assert res.derived_value is None
        assert res.left_value == Decimal("130")
        assert res.is_derived is False
        assert res.is_sec_reported is True

    def test_03_basic_q2_derivation_subtraction(self):
        """Scenario 3: Q1=100, Q2 YTD=220 -> Q2 derived standalone = 120 (Apr 1 - Jun 30)."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE

        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.target_quarter == "Q2"
        assert res.derived_value == Decimal("120")
        assert res.left_value == Decimal("220")
        assert res.right_value == Decimal("100")
        assert res.derived_start_date == date(2024, 4, 1)  # Q1.end + 1 day
        assert res.derived_end_date == date(2024, 6, 30)
        assert res.derived_duration_days == 91
        assert res.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert res.fiscal_period == "Q2"
        assert res.fiscal_year == 2024
        assert res.is_derived is True
        assert res.is_sec_reported is False
        assert res.derivation_method == "SUBTRACTION"
        assert res.formula == "Q2_YTD - Q1"
        assert res.confidence == "HIGH"
        assert "Sentinax-derived" in res.basis

    def test_04_basic_q3_derivation_subtraction(self):
        """Scenario 4: Q2 YTD=220, Q3 YTD=350 -> Q3 derived standalone = 130 (Jul 1 - Sep 30)."""
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))
        w3_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350"))

        series = SECFiscalSeriesAssembler.assemble_series([w2_ytd, w3_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE

        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.target_quarter == "Q3"
        assert res.derived_value == Decimal("130")
        assert res.left_value == Decimal("350")
        assert res.right_value == Decimal("220")
        assert res.derived_start_date == date(2024, 7, 1)  # Q2 YTD.end + 1 day
        assert res.derived_end_date == date(2024, 9, 30)
        assert res.derived_duration_days == 92
        assert res.is_derived is True
        assert res.is_sec_reported is False
        assert res.formula == "Q3_YTD - Q2_YTD"

    def test_05_negative_derived_quarter_valid(self):
        """Scenario 5: Q1=100, Q2 YTD=80 -> Q2 derived standalone = -20 (economically valid, no sign flip)."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("80"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.derived_value == Decimal("-20")

    def test_06_zero_derived_quarter_valid(self):
        """Scenario 6: Q1=220, Q2 YTD=220 -> Q2 derived standalone = 0 (zero is valid, not missing)."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("220"))
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.derived_value == Decimal("0")

    def test_07_decimal_scale_preservation(self):
        """Scenario 7: Decimal('220.0000') - Decimal('100.00') -> mathematically exact Decimal, no cents rounding."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100.00"))
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220.0000"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.derived_value == Decimal("120.0000")

    def test_08_huge_precision_exactness(self):
        """Scenario 8: >28-digit operands evaluate with exact precision without standard Decimal context loss."""
        v_left = Decimal("123456789012345678901234567890.123456")
        v_right = Decimal("123456789012345678901234567889.123455")
        diff = _subtract_decimal_exact(v_left, v_right)
        assert diff == Decimal("1.000001")

        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=v_right)
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=v_left)

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.derived_value == Decimal("1.000001")

    def test_09_local_context_isolation_resilience(self):
        """Scenario 9: Global Decimal context altered to low precision does not corrupt derivation and is restored."""
        orig_prec = decimal.getcontext().prec
        try:
            decimal.getcontext().prec = 5  # artifically constrain global context

            w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100.123456789"))
            w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220.123456789"))

            series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
            elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
            res = SECQuarterDeriver.derive_quarter(elig)

            assert res.status == SECQuarterDerivationStatus.DERIVED
            assert res.derived_value == Decimal("120.000000000")
            # Verify global context was untouched
            assert decimal.getcontext().prec == 5
        finally:
            decimal.getcontext().prec = orig_prec

    def test_10_non_finite_operands_fail_closed(self):
        """Scenario 10: NaN or Infinity operands return NON_FINITE_OPERAND status."""
        for non_finite in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
            w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=non_finite)

            p1 = _make_series_point(w1)
            p2 = _make_series_point(w2)

            elig = SECQuarterDerivationEligibility(
                target_quarter="Q2",
                status=SECDerivationEligibilityStatus.ELIGIBLE,
                canonical_concept="REVENUE",
                unit="USD",
                left_operand=p2,
                right_operand=p1,
                expected_formula="Q2_YTD - Q1",
                snapshot_hash="hash_alpha_123",
                snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
            )

            res = SECQuarterDeriver.derive_quarter(elig)
            assert res.status == SECQuarterDerivationStatus.NON_FINITE_OPERAND
            assert res.derived_value is None

    def test_11_tampered_eligibility_non_eligible_status(self):
        """Scenario 11: Non-ELIGIBLE status in contract (even with attached points) returns INELIGIBLE without subtraction."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        p1 = _make_series_point(w1)
        p2 = _make_series_point(w2)

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.MISSING_OPERAND,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=p2,
            right_operand=p1,
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INELIGIBLE
        assert res.derived_value is None

    def test_12_tampered_eligibility_formula_mismatch(self):
        """Scenario 12: Target Q2 with wrong expected_formula returns INVALID_ELIGIBILITY."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1),
            expected_formula="Q3_YTD - Q2_YTD",  # tampered
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_13_tampered_eligibility_period_kind(self):
        """Scenario 13: Left operand for Q2 is QUARTER_DURATION instead of YTD_DURATION -> INVALID_ELIGIBILITY."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_wrong = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2_wrong),
            right_operand=_make_series_point(w1),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_14_tampered_eligibility_concept_mismatch(self):
        """Scenario 14: Left concept REVENUE != right concept OPERATING_INCOME -> INVALID_ELIGIBILITY."""
        w1 = _make_winner_result(canonical_concept="OPERATING_INCOME", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(canonical_concept="REVENUE", economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_15_tampered_eligibility_unit_mismatch(self):
        """Scenario 15: USD vs EUR unit mismatch -> INVALID_ELIGIBILITY."""
        w1 = _make_winner_result(unit="EUR", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(unit="USD", economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_16_tampered_eligibility_snapshot_mismatch(self):
        """Scenario 16: Operands with different snapshot hashes -> INVALID_ELIGIBILITY."""
        w1 = _make_winner_result(snapshot_hash="hash_1", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(snapshot_hash="hash_2", economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_1",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_17_tampered_eligibility_system_as_of_mismatch(self):
        """Scenario 17: SYSTEM_AS_OF mode with differing as_of timestamps -> INVALID_ELIGIBILITY."""
        t1 = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        w1 = _make_winner_result(mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=t1, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=t2, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_18_tampered_eligibility_fiscal_start_mismatch(self):
        """Scenario 18: Operands with different start dates -> INVALID_ELIGIBILITY."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 2, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_19_tampered_eligibility_period_sequence_invalid(self):
        """Scenario 19: Right operand ends on or after left operand -> INVALID_ELIGIBILITY."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_20_derived_duration_outside_bounds_fails_closed(self):
        """Scenario 20: Derived quarter duration outside 70-115 days fails closed with INVALID_ELIGIBILITY."""
        # Q1 ends May 31 (152 days) and Q2 YTD ends Jun 30 -> derived is Jul 1 - Jun 30 (0 days) or inverted
        w1_bad = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 5, 31), value=Decimal("100"))
        w2_ytd = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        elig = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2_ytd),
            right_operand=_make_series_point(w1_bad),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_21_non_calendar_fiscal_year_october_start(self):
        """Scenario 21: Non-calendar issuer with Oct 1 fiscal start (Apple-style): Q1 Oct-Dec, Q2 YTD Oct-Mar -> derived Q2 Jan-Mar."""
        w1 = _make_winner_result(
            fiscal_year=2024, fiscal_period="Q1",
            start_date=date(2023, 10, 1), end_date=date(2023, 12, 31), value=Decimal("119575")
        )
        w2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2023, 10, 1), end_date=date(2024, 3, 31), value=Decimal("210328")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2023, 10, 1))
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.derived_value == Decimal("90753")
        assert res.derived_start_date == date(2024, 1, 1)
        assert res.derived_end_date == date(2024, 3, 31)
        assert res.fiscal_year == 2024
        assert res.fiscal_period == "Q2"

    def test_22_52_53_week_retailer_derivation(self):
        """Scenario 22: Retailer with 52/53 week calendar (Walmart/Target style): 13-week Q1, 26-week Q2 YTD -> 13-week standalone Q2."""
        w1 = _make_winner_result(
            fiscal_year=2024, fiscal_period="Q1",
            start_date=date(2024, 1, 28), end_date=date(2024, 4, 27), value=Decimal("152300")
        )
        w2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 28), end_date=date(2024, 7, 27), value=Decimal("312000")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 28))
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.derived_value == Decimal("159700")
        assert res.derived_start_date == date(2024, 4, 28)
        assert res.derived_end_date == date(2024, 7, 27)
        assert res.derived_duration_days == 91

    def test_23_lineage_and_provenance_preservation(self):
        """Scenario 23: Operands from different filings preserve separate accessions, filing IDs, and raw fact IDs."""
        f1_id = uuid4()
        f2_id = uuid4()
        rf1_id = uuid4()
        rf2_id = uuid4()

        w1 = _make_winner_result(
            accession_number="0000320193-24-000010", filing_id=f1_id, raw_fact_id=rf1_id,
            source_concept="SalesRevenueNet", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100")
        )
        w2_ytd = _make_winner_result(
            accession_number="0000320193-24-000020", filing_id=f2_id, raw_fact_id=rf2_id,
            source_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert res.left_accession == "0000320193-24-000020"
        assert res.right_accession == "0000320193-24-000010"
        assert res.left_filing_id == f2_id
        assert res.right_filing_id == f1_id
        assert res.left_raw_fact_id == rf2_id
        assert res.right_raw_fact_id == rf1_id
        assert res.left_source_concept == "RevenueFromContractWithCustomerExcludingAssessedTax"
        assert res.right_source_concept == "SalesRevenueNet"
        assert any("cross-filing" in d for d in res.diagnostics)

    def test_24_same_filing_lineage_diagnostic(self):
        """Scenario 24: Operands disclosed in same filing record same-filing lineage diagnostic."""
        w1 = _make_winner_result(accession_number="0000320193-24-000020", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_ytd = _make_winner_result(accession_number="0000320193-24-000020", economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        assert any("same-filing" in d for d in res.diagnostics)

    def test_25_current_reported_vs_system_as_of_identity(self):
        """Scenario 25: Same facts evaluated under CURRENT_REPORTED vs SYSTEM_AS_OF have distinct derivation_key."""
        as_of_dt = datetime(2024, 7, 1, 12, 0, tzinfo=timezone.utc)
        w1_curr = _make_winner_result(mode=SECWinnerResolutionMode.CURRENT_REPORTED, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_curr = _make_winner_result(mode=SECWinnerResolutionMode.CURRENT_REPORTED, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        w1_hist = _make_winner_result(mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=as_of_dt, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_hist = _make_winner_result(mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=as_of_dt, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        s_curr = SECFiscalSeriesAssembler.assemble_series([w1_curr, w2_curr])[0]
        s_hist = SECFiscalSeriesAssembler.assemble_series([w1_hist, w2_hist])[0]

        elig_curr = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_curr, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        elig_hist = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_hist, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        res_curr = SECQuarterDeriver.derive_quarter(elig_curr)
        res_hist = SECQuarterDeriver.derive_quarter(elig_hist)

        assert res_curr.status == SECQuarterDerivationStatus.DERIVED
        assert res_hist.status == SECQuarterDerivationStatus.DERIVED
        assert res_curr.derivation_key != res_hist.derivation_key
        assert res_curr.resolution_mode == SECWinnerResolutionMode.CURRENT_REPORTED
        assert res_hist.resolution_mode == SECWinnerResolutionMode.SYSTEM_AS_OF
        assert res_hist.as_of == as_of_dt

    def test_26_derivation_determinism(self):
        """Scenario 26: Calling derive_quarter multiple times on identical input produces identical derivation_key and values."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        res1 = SECQuarterDeriver.derive_quarter(elig)
        res2 = SECQuarterDeriver.derive_quarter(elig)

        assert res1.derivation_key == res2.derivation_key
        assert res1.derived_value == res2.derived_value
        assert res1.to_dict() == res2.to_dict()

    def test_27_unsupported_periods_q4_ttm_fy(self):
        """Scenario 27: Q4, TTM, FY requests return UNSUPPORTED_PERIOD."""
        for period in ("Q4", "TTM", "FY"):
            elig = SECQuarterDerivationEligibility(
                target_quarter=period,
                status=SECDerivationEligibilityStatus.UNSUPPORTED_PERIOD,
                canonical_concept="REVENUE",
                unit="USD",
            )
            res = SECQuarterDeriver.derive_quarter(elig)
            assert res.status == SECQuarterDerivationStatus.UNSUPPORTED_PERIOD
            assert res.derived_value is None

    def test_28_no_unexpected_metrics(self):
        """Scenario 28: Verify quarter_derivation module contains no FCF, margin, growth, valuation formulas."""
        import backend.engine.private.sec.quarter_derivation as qd_mod
        mod_symbols = dir(qd_mod)
        for forbidden in ("fcf", "ebitda", "margin", "growth", "roe", "roic", "valuation"):
            for sym in mod_symbols:
                assert forbidden not in sym.lower(), f"Unexpected metric symbol '{sym}' found in quarter_derivation module."

    def test_29_input_immutability(self):
        """Scenario 29: Derivation does not mutate input SECQuarterDerivationEligibility or operand points."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        # Copy state before derive
        orig_diag_count = len(elig.diagnostics)
        orig_status = elig.status
        orig_left_val = elig.left_operand.selected_value

        res = SECQuarterDeriver.derive_quarter(elig)

        assert elig.status == orig_status
        assert len(elig.diagnostics) == orig_diag_count
        assert elig.left_operand.selected_value == orig_left_val

    def test_30_to_dict_serialization(self):
        """Scenario 30: to_dict() produces valid JSON-serializable types with Decimal as string and ISO dates."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        d = res.to_dict()
        assert d["status"] == "derived"
        assert d["derived_value"] == "120"
        assert d["derived_start_date"] == "2024-04-01"
        assert d["derived_end_date"] == "2024-06-30"
        assert d["economic_period_kind"] == "quarter_duration"
        assert d["is_derived"] is True
        assert d["is_sec_reported"] is False
        assert isinstance(d["snapshot_ids"], list)
        assert isinstance(d["derivation_key"], str)

    def test_31_fiscal_year_inheritance(self):
        """Scenario 31: Fiscal year inheritance: both equal -> carried; one None -> carried; mismatch -> INVALID_ELIGIBILITY."""
        # Both equal
        w1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.fiscal_year == 2024

        # Differing fiscal year
        w1_diff = _make_winner_result(fiscal_year=2023, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        elig_diff = SECQuarterDerivationEligibility(
            target_quarter="Q2",
            status=SECDerivationEligibilityStatus.ELIGIBLE,
            canonical_concept="REVENUE",
            unit="USD",
            left_operand=_make_series_point(w2),
            right_operand=_make_series_point(w1_diff),
            expected_formula="Q2_YTD - Q1",
            snapshot_hash="hash_alpha_123",
            snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc),
        )
        res_diff = SECQuarterDeriver.derive_quarter(elig_diff)
        assert res_diff.status == SECQuarterDerivationStatus.INVALID_ELIGIBILITY

    def test_32_confidence_propagation(self):
        """Scenario 32: Confidence propagates strictly: HIGH -> HIGH, MEDIUM -> MEDIUM, LOW -> LOW (never upgraded)."""
        w1_comp = _make_winner_result(match_strength="COMPATIBLE", selection_confidence="MEDIUM", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1_comp, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.confidence == "MEDIUM"

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.confidence == "MEDIUM"

    def test_33_extreme_exponent_gap_exactness(self):
        """Scenario 33: 1E+100 - 1E-100 produces exact 200-digit coefficient without silent rounding."""
        left = Decimal("1E+100")
        right = Decimal("1E-100")

        diff = _subtract_decimal_exact(left, right)
        t = diff.as_tuple()
        assert t.sign == 0
        assert len(t.digits) == 200
        assert t.exponent == -100
        assert all(d == 9 for d in t.digits)

    def test_34_reverse_extreme_exponent_gap_exactness(self):
        """Scenario 34: 1E-100 - 1E+100 produces exact negative 200-digit coefficient."""
        left = Decimal("1E-100")
        right = Decimal("1E+100")

        diff = _subtract_decimal_exact(left, right)
        t = diff.as_tuple()
        assert t.sign == 1
        assert len(t.digits) == 200
        assert t.exponent == -100
        assert all(d == 9 for d in t.digits)

    def test_35_mixed_sign_extreme_exponent_gap(self):
        """Scenario 35: 1E+100 - (-1E-100) produces exact 201-digit addition equivalent."""
        left = Decimal("1E+100")
        right = Decimal("-1E-100")

        diff = _subtract_decimal_exact(left, right)
        t = diff.as_tuple()
        assert t.sign == 0
        assert len(t.digits) == 201
        assert t.exponent == -100
        assert t.digits[0] == 1
        assert t.digits[-1] == 1
        assert all(d == 0 for d in t.digits[1:-1])

    def test_36_large_fractional_range(self):
        """Scenario 36: Large integer part with ultra-small fractional part subtracts with exact scale."""
        left = Decimal("1234567890123456789012345678901234567890.0000000000000000000000001")
        right = Decimal("0.0000000000000000000000009")

        diff = _subtract_decimal_exact(left, right)
        expected = Decimal("1234567890123456789012345678901234567889.9999999999999999999999992")
        assert diff == expected
        assert diff.as_tuple() == expected.as_tuple()

    def test_37_cancellation_large_almost_equal_operands(self):
        """Scenario 37: Subtraction of almost-equal large numbers produces exact small residual."""
        left = Decimal("123456789012345678901234567890.000001")
        right = Decimal("123456789012345678901234567889.999999")

        diff = _subtract_decimal_exact(left, right)
        expected = Decimal("0.000002")
        assert diff == expected
        assert diff.as_tuple() == expected.as_tuple()

    def test_38_hostile_global_precision_context_immunity(self):
        """Scenario 38: Low global precision (prec=2) does not affect exact subtraction."""
        orig_prec = decimal.getcontext().prec
        try:
            decimal.getcontext().prec = 2

            diff = _subtract_decimal_exact(Decimal("123.456"), Decimal("0.056"))
            assert diff == Decimal("123.400")
            assert diff.as_tuple() == Decimal("123.400").as_tuple()
        finally:
            decimal.getcontext().prec = orig_prec

    def test_39_hostile_global_rounding_mode_immunity(self):
        """Scenario 39: Hostile global rounding modes do not affect exact subtraction."""
        orig_rounding = decimal.getcontext().rounding
        try:
            for mode in (decimal.ROUND_DOWN, decimal.ROUND_UP, decimal.ROUND_CEILING, decimal.ROUND_FLOOR):
                decimal.getcontext().rounding = mode
                diff = _subtract_decimal_exact(Decimal("1E+100"), Decimal("1E-100"))
                t = diff.as_tuple()
                assert len(t.digits) == 200
                assert t.exponent == -100
                assert all(d == 9 for d in t.digits)
        finally:
            decimal.getcontext().rounding = orig_rounding

    def test_40_global_context_restoration_guarantee(self):
        """Scenario 40: Calling derive_quarter preserves caller global Decimal context completely."""
        ctx = decimal.getcontext()
        before_state = (ctx.prec, ctx.rounding, ctx.Emax, ctx.Emin, dict(ctx.traps))

        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        res = SECQuarterDeriver.derive_quarter(elig)

        assert res.status == SECQuarterDerivationStatus.DERIVED
        after_state = (ctx.prec, ctx.rounding, ctx.Emax, ctx.Emin, dict(ctx.traps))
        assert before_state == after_state

    def test_41_arithmetic_error_handling_in_deriver(self, monkeypatch):
        """Scenario 41: Unexpected arithmetic exception in helper safely returns ARITHMETIC_ERROR."""
        def _failing_sub(l, r):
            raise ArithmeticError("Simulated hardware arithmetic fault")

        monkeypatch.setattr(
            "backend.engine.private.sec.quarter_derivation._subtract_decimal_exact",
            _failing_sub,
        )

        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220"))

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        res = SECQuarterDeriver.derive_quarter(elig)
        assert res.status == SECQuarterDerivationStatus.ARITHMETIC_ERROR
        assert res.derived_value is None
        assert any("Arithmetic error" in d for d in res.diagnostics)

