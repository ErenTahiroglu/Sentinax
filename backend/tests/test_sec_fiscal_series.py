"""
backend/tests/test_sec_fiscal_series.py
=========================================
Comprehensive Test Suite for SEC EDGAR Phase 8B.3A:
PIT Fiscal Series Assembly & Derivation Eligibility.

Tests:
    - Series Assembly Basics (1-9)
    - Q2 Derivation Eligibility (10-17)
    - Q3 Derivation Eligibility (18-22)
    - Non-Calendar Fiscal Year (23)
    - 52/53-Week Fiscal Year (24)
    - Semantic Quality & Confidence Propagation (25-28)
    - Duplicate Points & Conflict Resolution (29-31)
    - Q4 & TTM Out-Of-Scope Guards (32-34)
"""

import itertools
import pytest
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional
from uuid import UUID, uuid4

from backend.engine.private.sec.models import PeriodType
from backend.engine.private.sec.period_context import (
    SECEconomicPeriodKind,
    SECPeriodAlignmentStatus,
    SECPeriodizedFactCandidate,
)
from backend.engine.private.sec.winner_resolver import (
    SECWinnerResolutionMode,
    SECWinnerResolutionResult,
    SECWinnerStatus,
)
from backend.engine.private.sec.fiscal_series import (
    SECDerivationEligibilityStatus,
    SECFiscalSeriesStatus,
    SECFiscalSeriesConflict,
    SECFiscalSeries,
    SECFiscalSeriesPoint,
    SECQuarterDerivationEligibility,
    SECIntervalMatchResult,
    SECFiscalSeriesAssembler,
    SECFiscalSeriesEvaluator,
    _effective_inclusive_duration,
    _check_standalone_q2_interval,
    _check_standalone_q3_interval,
)

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
    value: Optional[Decimal] = Decimal("100.00"),
    mode: SECWinnerResolutionMode = SECWinnerResolutionMode.CURRENT_REPORTED,
    as_of: Optional[datetime] = None,
    status: SECWinnerStatus = SECWinnerStatus.SELECTED,
    snapshot_hash: str = "hash_alpha_123",
    snapshot_retrieved_at: Optional[datetime] = None,
    accession_number: str = "0000320193-24-000001",
    match_strength: str = "EXACT",
    selection_confidence: str = "HIGH",
    source_concept: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    is_comparative: bool = False,
    is_amendment: bool = False,
) -> SECWinnerResolutionResult:
    ret_at = snapshot_retrieved_at or datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc)
    snap_id = uuid4()

    dur_days: Optional[int]
    if duration_days is _UNSET:
        if start_date and end_date and end_date >= start_date:
            dur_days = (end_date - start_date).days + 1
        else:
            dur_days = None
    else:
        dur_days = duration_days

    if status == SECWinnerStatus.SELECTED:
        cand = SECPeriodizedFactCandidate(
            candidate_id=uuid4(),
            raw_fact_id=uuid4(),
            cik=cik,
            canonical_concept=canonical_concept,
            economic_period_kind=economic_period_kind,
            period_alignment_status=SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD,
            economic_start_date=start_date,
            economic_end_date=end_date,
            duration_days=dur_days,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing_id=uuid4(),
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
    else:
        cand = None

    return SECWinnerResolutionResult(
        mode=mode,
        status=status,
        cik=cik,
        economic_group_key=(cik, canonical_concept, unit, economic_period_kind.value, start_date.isoformat() if start_date else None, end_date.isoformat()),
        as_of=as_of,
        evaluation_snapshot_id=snap_id,
        evaluation_snapshot_ids=[snap_id],
        evaluation_snapshot_retrieved_at=ret_at,
        evaluation_snapshot_hash=snapshot_hash,
        selected_candidate=cand,
        selected_raw_fact_id=cand.raw_fact_id if cand else None,
        selected_value=value if status == SECWinnerStatus.SELECTED else None,
        selected_unit=unit,
        selected_source_concept=source_concept,
        selected_filing_id=cand.filing_id if cand else None,
        selected_accession_number=accession_number,
        selected_form=cand.form if cand else None,
        selection_confidence=selection_confidence,
        selection_basis="Selected test winner",
        diagnostics=["Winner result diagnostic"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Series Assembly Basics (Scenarios 1-9)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFiscalSeriesBasics:

    def test_01_same_mode_selected_points_assemble_properly(self):
        """Scenario 1: Verified SELECTED results for same concept/unit assemble into consistent series."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=181, value=Decimal("220")
        )

        series_list = SECFiscalSeriesAssembler.assemble_series([w1, w2])
        assert len(series_list) == 1
        s = series_list[0]
        assert s.cik == "0000320193"
        assert s.canonical_concept == "REVENUE"
        assert s.unit == "USD"
        assert len(s.points) == 2
        assert s.points[0].selected_value == Decimal("100")
        assert s.points[1].selected_value == Decimal("220")

    def test_02_failed_winner_retained_as_diagnostic_not_normal_point(self):
        """Scenario 2: Failed winner (e.g. AMBIGUOUS, NO_ELIGIBLE_CANDIDATE) is not added to points."""
        w_ok = _make_winner_result(fiscal_period="Q1", value=Decimal("100"))
        w_fail = _make_winner_result(
            fiscal_period="Q2",
            status=SECWinnerStatus.AMBIGUOUS_DISCLOSURE_ORDER,
            value=None,
        )

        series_list = SECFiscalSeriesAssembler.assemble_series([w_ok, w_fail])
        assert len(series_list) == 1
        s = series_list[0]
        assert len(s.points) == 1
        assert len(s.failed_results) == 1
        assert any("Excluded non-selected result" in d for d in s.diagnostics)

    def test_03_current_and_system_as_of_modes_are_strictly_separated(self):
        """Scenario 3: CURRENT_REPORTED and SYSTEM_AS_OF results cluster into distinct series."""
        as_of_time = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        w_curr = _make_winner_result(mode=SECWinnerResolutionMode.CURRENT_REPORTED)
        w_asof = _make_winner_result(mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=as_of_time)

        series_list = SECFiscalSeriesAssembler.assemble_series([w_curr, w_asof])
        assert len(series_list) == 2
        modes = {s.resolution_mode for s in series_list}
        assert modes == {SECWinnerResolutionMode.CURRENT_REPORTED, SECWinnerResolutionMode.SYSTEM_AS_OF}

    def test_04_system_as_of_different_timestamps_are_separated(self):
        """Scenario 4: SYSTEM_AS_OF results with different as_of timestamps separate."""
        t1 = datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        w1 = _make_winner_result(mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=t1)
        w2 = _make_winner_result(mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=t2)

        series_list = SECFiscalSeriesAssembler.assemble_series([w1, w2])
        assert len(series_list) == 2

    def test_05_different_snapshot_hash_are_separated(self):
        """Scenario 5: Results originating from different snapshot hashes form separate series."""
        w1 = _make_winner_result(snapshot_hash="hash_alpha")
        w2 = _make_winner_result(snapshot_hash="hash_beta")

        series_list = SECFiscalSeriesAssembler.assemble_series([w1, w2])
        assert len(series_list) == 2

    def test_06_logical_duplicate_snapshot_ids_same_hash_allowed_in_series(self):
        """Scenario 6: Logical duplicate snapshots with identical hash & retrieved_at assemble together."""
        ret_at = datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc)
        w1 = _make_winner_result(snapshot_hash="hash_same", snapshot_retrieved_at=ret_at, fiscal_period="Q1", value=Decimal("100"))
        w2 = _make_winner_result(
            snapshot_hash="hash_same", snapshot_retrieved_at=ret_at,
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=181, value=Decimal("220")
        )

        series_list = SECFiscalSeriesAssembler.assemble_series([w1, w2])
        assert len(series_list) == 1
        assert len(series_list[0].points) == 2

    def test_07_different_canonical_concepts_are_separated(self):
        """Scenario 7: REVENUE and OPERATING_INCOME cluster into separate fiscal series."""
        w_rev = _make_winner_result(canonical_concept="REVENUE")
        w_op = _make_winner_result(canonical_concept="OPERATING_INCOME_LOSS")

        series_list = SECFiscalSeriesAssembler.assemble_series([w_rev, w_op])
        assert len(series_list) == 2

    def test_08_different_units_are_separated(self):
        """Scenario 8: USD and EUR facts cluster into separate fiscal series."""
        w_usd = _make_winner_result(unit="USD")
        w_eur = _make_winner_result(unit="EUR")

        series_list = SECFiscalSeriesAssembler.assemble_series([w_usd, w_eur])
        assert len(series_list) == 2

    def test_09_instant_facts_not_derivation_eligible(self):
        """Scenario 9: Balance sheet / instant facts fail derivation with CONCEPT_MISMATCH."""
        w_cash = _make_winner_result(
            canonical_concept="CASH_AND_CASH_EQUIVALENTS",
            economic_period_kind=SECEconomicPeriodKind.INSTANT,
            start_date=None,
            end_date=date(2024, 3, 31),
            duration_days=None,
        )
        series_list = SECFiscalSeriesAssembler.assemble_series([w_cash])
        assert len(series_list) == 1
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series_list[0], "Q2")
        assert elig.status == SECDerivationEligibilityStatus.CONCEPT_MISMATCH


# ─────────────────────────────────────────────────────────────────────────────
# 2. Q2 Derivation Eligibility (Scenarios 10-17)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECQ2DerivationEligibility:

    def test_10_q1_quarter_and_q2_ytd_same_start_is_eligible(self):
        """Scenario 10: Q1 (QUARTER_DURATION) + Q2 YTD (YTD_DURATION) with same start -> ELIGIBLE Q2."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.expected_formula == "Q2_YTD - Q1"
        assert elig.confidence == "HIGH"
        assert elig.left_operand.selected_value == Decimal("220")
        assert elig.right_operand.selected_value == Decimal("100")

    def test_11_missing_q1_returns_missing_operand(self):
        """Scenario 11: Missing Q1 standalone quarter -> MISSING_OPERAND."""
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_12_missing_q2_ytd_returns_missing_operand(self):
        """Scenario 12: Missing Q2 YTD operand -> MISSING_OPERAND."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        series = SECFiscalSeriesAssembler.assemble_series([w1])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_13_different_fiscal_starts_returns_fiscal_start_mismatch(self):
        """Scenario 13: Q1 starts Jan 1, Q2 YTD starts Feb 1 -> FISCAL_START_MISMATCH or AMBIGUOUS_FISCAL_CHAIN."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 2, 1), end_date=date(2024, 6, 30), duration_days=151, value=Decimal("220")
        )
        # Create series manually to test evaluator pair check
        s = SECFiscalSeries(
            cik="0000320193", canonical_concept="REVENUE", unit="USD",
            resolution_mode=SECWinnerResolutionMode.CURRENT_REPORTED,
            evaluation_snapshot_hash="hash_same",
            evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc),
            points=[
                SECFiscalSeriesPoint(winner_result=w1, economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, fiscal_year=2024, fiscal_period="Q1", selected_value=Decimal("100"), selected_accession="0001", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=None, evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
                SECFiscalSeriesPoint(winner_result=w2, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, start_date=date(2024, 2, 1), end_date=date(2024, 6, 30), duration_days=151, fiscal_year=2024, fiscal_period="Q2", selected_value=Decimal("220"), selected_accession="0002", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=None, evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
            ]
        )
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s, "Q2")
        assert elig.status in (SECDerivationEligibilityStatus.FISCAL_START_MISMATCH, SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN)
        pair_elig = SECFiscalSeriesEvaluator._validate_derivation_pair(
            target_quarter="Q2", expected_formula="Q2_YTD - Q1", left=s.points[1], right=s.points[0], series=s
        )
        assert pair_elig.status == SECDerivationEligibilityStatus.FISCAL_START_MISMATCH

    def test_14_different_units_returns_unit_mismatch(self):
        """Scenario 14: Left and right operands have differing units -> UNIT_MISMATCH."""
        w1 = _make_winner_result(unit="USD", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(
            unit="EUR",
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        s = SECFiscalSeries(
            cik="0000320193", canonical_concept="REVENUE", unit="USD",
            resolution_mode=SECWinnerResolutionMode.CURRENT_REPORTED,
            evaluation_snapshot_hash="hash_same",
            evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc),
            points=[
                SECFiscalSeriesPoint(winner_result=w1, economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, fiscal_year=2024, fiscal_period="Q1", selected_value=Decimal("100"), selected_accession="0001", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=None, evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
                SECFiscalSeriesPoint(winner_result=w2, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, fiscal_year=2024, fiscal_period="Q2", selected_value=Decimal("220"), selected_accession="0002", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=None, evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
            ]
        )
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s, "Q2")
        assert elig.status == SECDerivationEligibilityStatus.UNIT_MISMATCH

    def test_15_different_snapshot_hashes_returns_snapshot_mismatch(self):
        """Scenario 15: Left and right operands have differing snapshot hashes -> SNAPSHOT_MISMATCH."""
        w1 = _make_winner_result(snapshot_hash="hash_A", fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(
            snapshot_hash="hash_B",
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        s = SECFiscalSeries(
            cik="0000320193", canonical_concept="REVENUE", unit="USD",
            resolution_mode=SECWinnerResolutionMode.CURRENT_REPORTED,
            points=[
                SECFiscalSeriesPoint(winner_result=w1, economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, fiscal_year=2024, fiscal_period="Q1", selected_value=Decimal("100"), selected_accession="0001", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, tzinfo=timezone.utc), evaluation_snapshot_hash="hash_A", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
                SECFiscalSeriesPoint(winner_result=w2, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, fiscal_year=2024, fiscal_period="Q2", selected_value=Decimal("220"), selected_accession="0002", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, tzinfo=timezone.utc), evaluation_snapshot_hash="hash_B", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
            ]
        )
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s, "Q2")
        assert elig.status == SECDerivationEligibilityStatus.SNAPSHOT_MISMATCH

    def test_16_actual_standalone_q2_exists_returns_original_available(self):
        """Scenario 16: Actual standalone Q2 QUARTER_DURATION point exists -> ORIGINAL_AVAILABLE."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_standalone = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_standalone, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE

    def test_17_zero_q1_is_valid_operand(self):
        """Scenario 17: Selected Decimal('0') is a valid non-missing operand."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("0.00"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220.00")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.right_operand.selected_value == Decimal("0.00")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Q3 Derivation Eligibility (Scenarios 18-22)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECQ3DerivationEligibility:

    def test_18_q2_ytd_and_q3_ytd_same_start_is_eligible(self):
        """Scenario 18: Q2 YTD (YTD_DURATION) + Q3 YTD (YTD_DURATION) with same start -> ELIGIBLE Q3."""
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w2, w3])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.expected_formula == "Q3_YTD - Q2_YTD"
        assert elig.left_operand.selected_value == Decimal("350")
        assert elig.right_operand.selected_value == Decimal("220")

    def test_19_missing_q2_ytd_returns_missing_operand_for_q3(self):
        """Scenario 19: Missing Q2 YTD operand -> MISSING_OPERAND for Q3 derivation."""
        w3 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w3])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_20_q3_end_before_or_equal_to_q2_end_returns_period_sequence_invalid(self):
        """Scenario 20: Q3 end <= Q2 end -> PERIOD_SEQUENCE_INVALID."""
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3_bad = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        s = SECFiscalSeries(
            cik="0000320193", canonical_concept="REVENUE", unit="USD",
            resolution_mode=SECWinnerResolutionMode.CURRENT_REPORTED,
            evaluation_snapshot_hash="hash_same",
            evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc),
            points=[
                SECFiscalSeriesPoint(winner_result=w3_bad, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, fiscal_year=2024, fiscal_period="Q3", selected_value=Decimal("220"), selected_accession="0003", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, tzinfo=timezone.utc), evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
                SECFiscalSeriesPoint(winner_result=w2, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, fiscal_year=2024, fiscal_period="Q2", selected_value=Decimal("220"), selected_accession="0002", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, tzinfo=timezone.utc), evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
            ]
        )
        pair_elig = SECFiscalSeriesEvaluator._validate_derivation_pair(
            target_quarter="Q3", expected_formula="Q3_YTD - Q2_YTD", left=s.points[0], right=s.points[1], series=s
        )
        assert pair_elig.status == SECDerivationEligibilityStatus.PERIOD_SEQUENCE_INVALID

    def test_21_fiscal_starts_differ_for_q3_returns_fiscal_start_mismatch(self):
        """Scenario 21: Q2 YTD and Q3 YTD have differing start dates -> FISCAL_START_MISMATCH or AMBIGUOUS_FISCAL_CHAIN."""
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 2, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        s = SECFiscalSeries(
            cik="0000320193", canonical_concept="REVENUE", unit="USD",
            resolution_mode=SECWinnerResolutionMode.CURRENT_REPORTED,
            evaluation_snapshot_hash="hash_same",
            evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc),
            points=[
                SECFiscalSeriesPoint(winner_result=w3, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, start_date=date(2024, 2, 1), end_date=date(2024, 9, 30), duration_days=243, fiscal_year=2024, fiscal_period="Q3", selected_value=Decimal("350"), selected_accession="0003", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, tzinfo=timezone.utc), evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
                SECFiscalSeriesPoint(winner_result=w2, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION, start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, fiscal_year=2024, fiscal_period="Q2", selected_value=Decimal("220"), selected_accession="0002", selected_filing_id=None, evaluation_snapshot_id=None, evaluation_snapshot_ids=[], evaluation_snapshot_retrieved_at=datetime(2024, 10, 1, tzinfo=timezone.utc), evaluation_snapshot_hash="hash_same", source_concept=None, match_strength="EXACT", selection_confidence="HIGH", is_comparative=False, is_amendment=False),
            ]
        )
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s, "Q3")
        assert elig.status in (SECDerivationEligibilityStatus.FISCAL_START_MISMATCH, SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN)
        pair_elig = SECFiscalSeriesEvaluator._validate_derivation_pair(
            target_quarter="Q3", expected_formula="Q3_YTD - Q2_YTD", left=s.points[0], right=s.points[1], series=s
        )
        assert pair_elig.status == SECDerivationEligibilityStatus.FISCAL_START_MISMATCH


    def test_22_original_standalone_q3_exists_returns_original_available(self):
        """Scenario 22: Original standalone Q3 QUARTER_DURATION exists -> ORIGINAL_AVAILABLE."""
        w3_standalone = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), value=Decimal("130")
        )
        w3_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w3_standalone, w3_ytd, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE


# ─────────────────────────────────────────────────────────────────────────────
# 4. Non-Calendar & 52/53-Week Fiscal Years (Scenarios 23-24)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECNonCalendarAnd53Week:

    def test_23_non_calendar_fiscal_year_october_start(self):
        """Scenario 23: Fiscal year starts Oct 1 (Q1 Oct-Dec, Q2 YTD Oct-Mar, Q3 YTD Oct-Jun)."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2023, 10, 1), end_date=date(2023, 12, 31), duration_days=92, value=Decimal("100"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2023, 10, 1), end_date=date(2024, 3, 31), duration_days=183, value=Decimal("210")
        )
        w3 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2023, 10, 1), end_date=date(2024, 6, 30), duration_days=274, value=Decimal("330")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2, w3])[0]

        elig_q2 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2023, 10, 1))
        assert elig_q2.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig_q2.expected_formula == "Q2_YTD - Q1"

        elig_q3 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2023, 10, 1))
        assert elig_q3.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig_q3.expected_formula == "Q3_YTD - Q2_YTD"

    def test_24_53_week_retailer_fiscal_year(self):
        """Scenario 24: Retailer with 52/53-week fiscal year (e.g. 13-week Q1, 26-week Q2 YTD, 39-week Q3 YTD)."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 28), end_date=date(2024, 4, 27), duration_days=91, value=Decimal("100"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 28), end_date=date(2024, 7, 27), duration_days=182, value=Decimal("215")
        )
        w3 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 28), end_date=date(2024, 10, 26), duration_days=273, value=Decimal("340")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2, w3])[0]

        elig_q2 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 28))
        assert elig_q2.status == SECDerivationEligibilityStatus.ELIGIBLE

        elig_q3 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 28))
        assert elig_q3.status == SECDerivationEligibilityStatus.ELIGIBLE


# ─────────────────────────────────────────────────────────────────────────────
# 5. Semantic Quality & Confidence Propagation (Scenarios 25-28)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSemanticQualityAndConfidence:

    def test_25_exact_and_exact_yields_high_confidence(self):
        """Scenario 25: Both operands EXACT and HIGH selection confidence -> HIGH eligibility confidence."""
        w1 = _make_winner_result(match_strength="EXACT", selection_confidence="HIGH", fiscal_period="Q1")
        w2 = _make_winner_result(
            match_strength="EXACT", selection_confidence="HIGH",
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.confidence == "HIGH"

    def test_26_exact_and_compatible_yields_medium_confidence(self):
        """Scenario 26: One operand is COMPATIBLE alias -> MEDIUM eligibility confidence with diagnostic."""
        w1 = _make_winner_result(match_strength="COMPATIBLE", selection_confidence="HIGH", fiscal_period="Q1")
        w2 = _make_winner_result(
            match_strength="EXACT", selection_confidence="HIGH",
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.confidence == "MEDIUM"
        assert any("compatible semantic alias" in d for d in elig.diagnostics)

    def test_27_unsafe_legacy_combination_returns_semantic_quality_risk(self):
        """Scenario 27: Operands with LEGACY_COMPATIBLE variant fail with SEMANTIC_QUALITY_RISK."""
        w1 = _make_winner_result(match_strength="LEGACY_COMPATIBLE", selection_confidence="HIGH", fiscal_period="Q1")
        w2 = _make_winner_result(
            match_strength="EXACT", selection_confidence="HIGH",
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.SEMANTIC_QUALITY_RISK

    def test_28_eligibility_confidence_never_exceeds_weakest_operand(self):
        """Scenario 28: Left operand HIGH, right operand MEDIUM -> derived confidence is MEDIUM."""
        w1 = _make_winner_result(match_strength="EXACT", selection_confidence="MEDIUM", fiscal_period="Q1")
        w2 = _make_winner_result(
            match_strength="EXACT", selection_confidence="HIGH",
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.confidence == "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Duplicate Points & Conflicts (Scenarios 29-31)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECDuplicatePointsAndConflicts:

    def test_29_identical_duplicate_same_value_deduplicates(self):
        """Scenario 29: Duplicate winner results with same period and same value deduplicate into single point."""
        w1_a = _make_winner_result(fiscal_period="Q1", value=Decimal("100"), accession_number="0001")
        w1_b = _make_winner_result(fiscal_period="Q1", value=Decimal("100"), accession_number="0002")

        series = SECFiscalSeriesAssembler.assemble_series([w1_a, w1_b])[0]
        assert len(series.points) == 1
        assert series.points[0].selected_value == Decimal("100")

    def test_30_identical_period_differing_values_creates_series_conflict(self):
        """Scenario 30: Duplicate winner results with same period and differing values cause fail-closed conflict."""
        w1_a = _make_winner_result(fiscal_period="Q1", value=Decimal("100"), accession_number="0001")
        w1_b = _make_winner_result(fiscal_period="Q1", value=Decimal("105"), accession_number="0002")

        series = SECFiscalSeriesAssembler.assemble_series([w1_a, w1_b])[0]
        # Both conflicting points are excluded from valid points
        assert len(series.points) == 0
        assert any("Series conflict" in d for d in series.diagnostics)

    def test_31_input_order_reversal_yields_identical_series_result(self):
        """Scenario 31: Reversing input winner results list produces identical assembled series."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        s1 = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        s2 = SECFiscalSeriesAssembler.assemble_series([w2, w1])[0]

        assert len(s1.points) == len(s2.points) == 2
        assert s1.points[0].selected_value == s2.points[0].selected_value
        assert s1.points[1].selected_value == s2.points[1].selected_value


# ─────────────────────────────────────────────────────────────────────────────
# 7. Q4 & TTM Out-of-Scope Guards (Scenarios 32-34)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECQ4AndTTMGuards:

    def test_32_q4_request_returns_unsupported_period(self):
        """Scenario 32: Q4 derivation request explicitly returns UNSUPPORTED_PERIOD."""
        w1 = _make_winner_result(fiscal_period="Q1")
        series = SECFiscalSeriesAssembler.assemble_series([w1])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q4")

        assert elig.status == SECDerivationEligibilityStatus.UNSUPPORTED_PERIOD

    def test_33_ttm_request_returns_unsupported_period(self):
        """Scenario 33: TTM derivation request explicitly returns UNSUPPORTED_PERIOD."""
        w1 = _make_winner_result(fiscal_period="Q1")
        series = SECFiscalSeriesAssembler.assemble_series([w1])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "TTM")

        assert elig.status == SECDerivationEligibilityStatus.UNSUPPORTED_PERIOD

    def test_34_no_arithmetic_subtraction_performed_in_eligibility_phase(self):
        """Scenario 34: SECQuarterDerivationEligibility has no numerical derived value field."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        # Verify that no derived value attribute exists on eligibility result
        assert not hasattr(elig, "derived_value")


# ─────────────────────────────────────────────────────────────────────────────
# 8. Phase 8B.3A.5 Hardening (Scenarios 35-46)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFiscalSeriesHardening8B3A5:

    def test_35_missing_economic_end_date_excluded_without_1970_sentinel(self):
        """Scenario 30: Selected winner lacking economic_end_date does not create point with fabricated 1970 date."""
        w = _make_winner_result(fiscal_period="Q1", value=Decimal("100"))
        # Force cand.economic_end_date = None
        w.selected_candidate.economic_end_date = None

        series = SECFiscalSeriesAssembler.assemble_series([w])[0]
        assert len(series.points) == 0
        assert len(series.failed_results) == 1
        assert any("lacks economic_end_date" in d for d in series.diagnostics)

    def test_36_global_no_1970_sentinel_in_fiscal_series_source(self):
        """Scenario 31: Verify fiscal_series.py contains zero 1970 sentinel date fabrication."""
        import inspect
        import backend.engine.private.sec.fiscal_series as fs_mod
        source = inspect.getsource(fs_mod)
        assert "1970" not in source
        assert "date(1970" not in source

    def test_37_series_with_only_q2_standalone_evaluating_q1_returns_missing_operand(self):
        """Scenario 13: Series containing only Q2 standalone (Apr-Jun) evaluating Q1 returns MISSING_OPERAND, not ORIGINAL_AVAILABLE."""
        w_q2_standalone = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w_q2_standalone])[0]
        # Target fiscal start is Jan 1
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q1", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_38_cross_quarter_misidentification_q3_not_treated_as_q2(self):
        """Scenario 17: Series contains Q3 standalone (Jul-Sep), evaluate Q2 returns MISSING_OPERAND (not ORIGINAL Q2)."""
        w_q3_standalone = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), duration_days=91, value=Decimal("130")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w_q3_standalone])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status != SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE
        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_39_reverse_misidentification_q2_not_treated_as_q3(self):
        """Scenario 18: Series contains Q2 standalone (Apr-Jun), evaluate Q3 returns MISSING_OPERAND (not ORIGINAL Q3)."""
        w_q2_standalone = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w_q2_standalone])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status != SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE
        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_40_multi_year_series_without_explicit_anchor_returns_ambiguous_fiscal_chain(self):
        """Scenario 21: Series contains FY2023 (Q1+Q2 YTD) and FY2024 (Q1+Q2 YTD). Calling evaluate Q2 without anchor returns AMBIGUOUS_FISCAL_CHAIN."""
        w23_q1 = _make_winner_result(fiscal_year=2023, fiscal_period="Q1", start_date=date(2023, 1, 1), end_date=date(2023, 3, 31), duration_days=90, value=Decimal("90"))
        w23_q2_ytd = _make_winner_result(
            fiscal_year=2023, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2023, 1, 1), end_date=date(2023, 6, 30), duration_days=181, value=Decimal("190")
        )
        w24_q1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w24_q2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w23_q1, w23_q2_ytd, w24_q1, w24_q2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN

    def test_41_multi_year_series_with_explicit_anchor_resolves_correctly(self):
        """Scenario 22: Multi-year series with target_fiscal_start_date=2024-01-01 resolves only 2024 operands."""
        w23_q1 = _make_winner_result(fiscal_year=2023, fiscal_period="Q1", start_date=date(2023, 1, 1), end_date=date(2023, 3, 31), duration_days=90, value=Decimal("90"))
        w23_q2_ytd = _make_winner_result(
            fiscal_year=2023, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2023, 1, 1), end_date=date(2023, 6, 30), duration_days=181, value=Decimal("190")
        )
        w24_q1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w24_q2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w23_q1, w23_q2_ytd, w24_q1, w24_q2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.left_operand.selected_value == Decimal("220")
        assert elig.right_operand.selected_value == Decimal("100")

    def test_42_same_fiscal_year_metadata_with_differing_start_dates_returns_ambiguous(self):
        """Scenario 23: Multiple distinct economic_start_dates under same fiscal_year metadata returns AMBIGUOUS_FISCAL_CHAIN."""
        w1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2 = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 2, 1), end_date=date(2024, 7, 31), value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1, w2])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_year=2024)

        assert elig.status == SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN

    def test_43_fp_contradiction_fails_closed(self):
        """Scenarios 24 & 25: Point has Q1 dates (Jan-Mar) but fp='Q3'. Evaluated for Q3 returns MISSING_OPERAND, for Q1 returns PERIOD_IDENTITY_UNRESOLVED."""
        w_bad_fp = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w_bad_fp])[0]

        # Evaluating for Q3 does not treat Jan-Mar as Q3
        elig_q3 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_q3.status == SECDerivationEligibilityStatus.MISSING_OPERAND

        # Evaluating for Q1 flags the contradiction
        elig_q1 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q1", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_q1.status == SECDerivationEligibilityStatus.PERIOD_IDENTITY_UNRESOLVED

    def test_44_series_conflict_on_standalone_q2_blocks_derivation_fallback(self):
        """Scenario 27: Two conflicting standalone Q2 winners with values 120 and 125 + valid Q1 and Q2 YTD -> returns SERIES_CONFLICT (never ELIGIBLE Q2_YTD - Q1)."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        w2_stand_a = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120"), accession_number="0001"
        )
        w2_stand_b = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("125"), accession_number="0002"
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w2_stand_a, w2_stand_b])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED
        assert len(series.conflicts) == 1

        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.SERIES_CONFLICT

    def test_45_series_conflict_on_operand_blocks_derivation(self):
        """Scenario 28: Two conflicting Q1 points + valid Q2 YTD -> returns SERIES_CONFLICT (not MISSING_OPERAND, not ELIGIBLE)."""
        w1_a = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"), accession_number="0001")
        w1_b = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("105"), accession_number="0002")
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1_a, w1_b, w2_ytd])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED

        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.SERIES_CONFLICT

    def test_46_conflict_on_unrelated_period_does_not_block_clean_target_chain(self):
        """Scenario 29: Conflict in FY2022 Q1 does not block clean FY2024 Q2 derivation."""
        # FY2022 Q1 conflicting points
        w22_q1_a = _make_winner_result(fiscal_year=2022, fiscal_period="Q1", start_date=date(2022, 1, 1), end_date=date(2022, 3, 31), duration_days=90, value=Decimal("80"), accession_number="0001")
        w22_q1_b = _make_winner_result(fiscal_year=2022, fiscal_period="Q1", start_date=date(2022, 1, 1), end_date=date(2022, 3, 31), duration_days=90, value=Decimal("85"), accession_number="0002")

        # FY2024 clean points
        w24_q1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w24_q2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w22_q1_a, w22_q1_b, w24_q1, w24_q2_ytd])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED
        assert len(series.conflicts) == 1

        # Target FY2024 is evaluated cleanly
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.left_operand.selected_value == Decimal("220")
        assert elig.right_operand.selected_value == Decimal("100")


# ─────────────────────────────────────────────────────────────────────────────
# 9. Phase 8B.3A.6 Hardening (Scenarios 47-59)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFiscalSeriesHardening8B3A6:

    def test_47_target_fiscal_year_normal_chain_original_q2_available(self):
        """Scenario 27: FY2024 (Q1 Jan-Mar, Q2 YTD Jan-Jun, Q2 standalone Apr-Jun) evaluate Q2 with target_fiscal_year=2024 -> ORIGINAL_AVAILABLE."""
        w1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        w2_standalone = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w2_standalone])[0]
        # Standalone Apr 1 start date must NOT create ambiguity with Jan 1 fiscal start
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_year=2024)

        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE
        assert elig.left_operand.selected_value == Decimal("120")

    def test_48_auto_q2_normal_chain_original_available(self):
        """Scenario 28: Same FY2024 data without target start or year -> single Jan 1 chain inferred -> ORIGINAL_AVAILABLE."""
        w1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        w2_standalone = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w2_standalone])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE
        assert elig.left_operand.selected_value == Decimal("120")

    def test_49_auto_q2_derivation_chain_eligible(self):
        """Scenario 29: Q1 Jan-Mar + Q2 YTD Jan-Jun, no standalone, no explicit target -> ELIGIBLE."""
        w1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")

        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.expected_formula == "Q2_YTD - Q1"
        assert elig.left_operand.selected_value == Decimal("220")
        assert elig.right_operand.selected_value == Decimal("100")

    def test_50_target_fiscal_year_q3_normal_chain_original_available(self):
        """Scenario 30: Q2 YTD Jan-Jun + Q3 YTD Jan-Sep + Q3 standalone Jul-Sep -> ORIGINAL_AVAILABLE Q3."""
        w2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        w3_standalone = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), value=Decimal("130")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w2_ytd, w3_ytd, w3_standalone])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_year=2024)

        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE
        assert elig.left_operand.selected_value == Decimal("130")

    def test_51_auto_q3_normal_chain_original_available(self):
        """Scenario 30 auto: Same Q3 data without target -> single Jan 1 chain -> ORIGINAL_AVAILABLE."""
        w2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        w3_standalone = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), value=Decimal("130")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w2_ytd, w3_ytd, w3_standalone])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3")

        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE
        assert elig.left_operand.selected_value == Decimal("130")

    def test_52_real_multi_chain_ambiguity_under_same_fiscal_year(self):
        """Scenario 31: True collision with Q1 Jan 1 and another Q1 Feb 1 under same fiscal_year -> AMBIGUOUS_FISCAL_CHAIN."""
        w1_a = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w1_b = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 2, 1), end_date=date(2024, 4, 30), value=Decimal("105"))

        series = SECFiscalSeriesAssembler.assemble_series([w1_a, w1_b])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q1", target_fiscal_year=2024)

        assert elig.status == SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN

    def test_53_corrupted_duration_none_fails_closed(self):
        """Scenario 32: Missing or invalid duration points cannot prove eligibility merely from fp."""
        # Point where duration calculation is invalid / out of bounds
        w2_bad = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 3, 1), duration_days=60, value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w2_bad])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        # 60 days is not 6M YTD (150-210d)
        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_54_central_boundaries_consistency(self):
        """Scenario 33: Verify exact centralized bounds (150-210 for Q2, 240-300 for Q3)."""
        # Q2 YTD exact lower bound: 150 days (Jan 1 to May 29 inclusive = 150d)
        w_150 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            start_date=date(2024, 1, 1), end_date=date(2024, 5, 29), duration_days=150, value=Decimal("150")
        )
        # Q2 YTD exact upper bound: 210 days (Jan 1 to Jul 28 inclusive = 210d)
        w_210 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            start_date=date(2024, 1, 1), end_date=date(2024, 7, 28), duration_days=210, value=Decimal("210")
        )
        # Q2 YTD out of bounds: 149 days (Jan 1 to May 28 inclusive = 149d)
        w_149 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            start_date=date(2024, 1, 1), end_date=date(2024, 5, 28), duration_days=149, value=Decimal("149")
        )
        # Q2 YTD out of bounds: 211 days (Jan 1 to Jul 29 inclusive = 211d)
        w_211 = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            start_date=date(2024, 1, 1), end_date=date(2024, 7, 29), duration_days=211, value=Decimal("211")
        )

        w1_anchor = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))

        # 150d is accepted
        s150 = SECFiscalSeriesAssembler.assemble_series([w1_anchor, w_150])[0]
        assert SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s150, "Q2", target_fiscal_start_date=date(2024, 1, 1)).status == SECDerivationEligibilityStatus.ELIGIBLE

        # 210d is accepted
        s210 = SECFiscalSeriesAssembler.assemble_series([w1_anchor, w_210])[0]
        assert SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s210, "Q2", target_fiscal_start_date=date(2024, 1, 1)).status == SECDerivationEligibilityStatus.ELIGIBLE

        # 149d is rejected (missing Q2 YTD)
        s149 = SECFiscalSeriesAssembler.assemble_series([w1_anchor, w_149])[0]
        assert SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s149, "Q2", target_fiscal_start_date=date(2024, 1, 1)).status == SECDerivationEligibilityStatus.MISSING_OPERAND

        # 211d is rejected (missing Q2 YTD)
        s211 = SECFiscalSeriesAssembler.assemble_series([w1_anchor, w_211])[0]
        assert SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s211, "Q2", target_fiscal_start_date=date(2024, 1, 1)).status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_55_relational_q2_original_standalone_identity(self):
        """Scenario 34: Q1 end Mar 31, Q2 YTD end Jun 30, standalone Apr 1 - Jun 30 -> ORIGINAL_AVAILABLE. Standalone May 1 - Jul 30 -> NOT Q2."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        # Standalone matching Q2 endpoint exactly
        w2_match = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )
        # Standalone with shifted non-matching endpoint
        w2_shifted = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 5, 1), end_date=date(2024, 7, 30), duration_days=91, value=Decimal("125")
        )

        s_match = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w2_match])[0]
        elig_match = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_match, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_match.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE

        s_shifted = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w2_shifted])[0]
        elig_shifted = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_shifted, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        # Shifted standalone does not match Q2 YTD endpoint, falls back to YTD derivation
        assert elig_shifted.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig_shifted.expected_formula == "Q2_YTD - Q1"

    def test_56_relational_q3_original_standalone_identity(self):
        """Scenario 35: Q2 YTD end Jun 30, Q3 YTD end Sep 30, standalone Jul 1 - Sep 30 -> ORIGINAL_AVAILABLE. Standalone Apr 1 - Jun 30 -> NOT Q3."""
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        w3_match = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), value=Decimal("130")
        )

        s_match = SECFiscalSeriesAssembler.assemble_series([w2_ytd, w3_ytd, w3_match])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_match, "Q3", target_fiscal_start_date=date(2024, 1, 1))
        assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE

    def test_57_conflict_identity_isolation_q2_and_q3(self):
        """Scenario 36: Standalone Q3 conflict (Jul-Sep) blocks Q3, but not Q2 derivation. Standalone Q2 conflict blocks Q2, but not Q3."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), value=Decimal("100"))
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )

        # Standalone Q3 conflicting facts
        w3_stand_a = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), value=Decimal("130"), accession_number="0001"
        )
        w3_stand_b = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), value=Decimal("135"), accession_number="0002"
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w3_ytd, w3_stand_a, w3_stand_b])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED
        assert len(series.conflicts) == 1

        # Q3 evaluation is blocked by standalone Q3 conflict
        elig_q3 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_q3.status == SECDerivationEligibilityStatus.SERIES_CONFLICT

        # Q2 derivation is NOT blocked by standalone Q3 conflict!
        elig_q2 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_q2.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig_q2.left_operand.selected_value == Decimal("220")
        assert elig_q2.right_operand.selected_value == Decimal("100")

    def test_58_order_independence_on_chains_and_ambiguity(self):
        """Scenario 37: Permuting winner result order yields identical eligibility outcome."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        w2_standalone = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )

        items = [w1, w2_ytd, w2_standalone]
        for perm in itertools.permutations(items):
            series = SECFiscalSeriesAssembler.assemble_series(list(perm))[0]
            elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2")
            assert elig.status == SECDerivationEligibilityStatus.ORIGINAL_AVAILABLE
            assert elig.left_operand.selected_value == Decimal("120")

    def test_59_cardinality_missing_one_side_multiple_on_other(self):
        """Scenario 24/25/26: Missing Q1 with multiple Q2 YTD returns AMBIGUOUS_FISCAL_CHAIN, not arbitrary left_operand."""
        w2_ytd_a = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220"), accession_number="0001"
        )
        w2_ytd_b = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 25), duration_days=177, value=Decimal("218"), accession_number="0002"
        )

        series = SECFiscalSeriesAssembler.assemble_series([w2_ytd_a, w2_ytd_b])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        assert elig.status == SECDerivationEligibilityStatus.AMBIGUOUS_FISCAL_CHAIN


# ─────────────────────────────────────────────────────────────────────────────
# 10. Phase 8B.3A.7 Hardening (Scenarios 60-70)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFiscalSeriesHardening8B3A7:

    def test_60_duration_consistency_authoritative_rule(self):
        """Scenario 38: Authoritative duration rule and duration_days=None policy."""
        # Case A: dates 182d, duration_days=182 -> valid
        assert _effective_inclusive_duration(date(2024, 1, 1), date(2024, 6, 30), 182) == 182

        # Case B: dates 182d, duration_days=None -> valid, computed
        assert _effective_inclusive_duration(date(2024, 1, 1), date(2024, 6, 30), None) == 182

        # Case C: dates 182d, duration_days=90 -> mismatch, fails closed (returns None)
        assert _effective_inclusive_duration(date(2024, 1, 1), date(2024, 6, 30), 90) is None

        # Case D: invalid dates end < start -> returns None
        assert _effective_inclusive_duration(date(2024, 6, 30), date(2024, 1, 1), 182) is None

    def test_61_duration_mismatch_fails_closed_in_derivation_eligibility(self):
        """Scenario 5: Point with duration_days mismatch fails closed, fp cannot bypass."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        # Mismatched duration_days: dates are 182d, but duration_days is declared as 90d
        w2_bad = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=90, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_bad])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        # Because w2_bad duration is inconsistent, it cannot qualify as Q2 YTD operand -> MISSING_OPERAND
        assert elig.status == SECDerivationEligibilityStatus.MISSING_OPERAND

    def test_62_structured_conflict_fiscal_metadata_preservation(self):
        """Scenario 8: Structured conflict preserves candidate fiscal_year and fiscal_period metadata."""
        w_a = _make_winner_result(fiscal_year=2022, fiscal_period="Q2", start_date=date(2022, 4, 1), end_date=date(2022, 6, 30), duration_days=91, value=Decimal("100"), accession_number="0001")
        w_b = _make_winner_result(fiscal_year=2022, fiscal_period="Q2", start_date=date(2022, 4, 1), end_date=date(2022, 6, 30), duration_days=91, value=Decimal("105"), accession_number="0002")

        series = SECFiscalSeriesAssembler.assemble_series([w_a, w_b])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED
        assert len(series.conflicts) == 1

        c = series.conflicts[0]
        assert c.fiscal_year == 2022
        assert c.fiscal_period == "Q2"
        assert c.fiscal_years == [2022]
        assert c.fiscal_periods == ["Q2"]

    def test_63_target_fiscal_year_isolates_unrelated_standalone_conflict(self):
        """Scenario 14: FY2022 standalone Q2 conflict does not block clean FY2024 target evaluation."""
        w22_a = _make_winner_result(fiscal_year=2022, fiscal_period="Q2", start_date=date(2022, 4, 1), end_date=date(2022, 6, 30), duration_days=91, value=Decimal("80"), accession_number="0001")
        w22_b = _make_winner_result(fiscal_year=2022, fiscal_period="Q2", start_date=date(2022, 4, 1), end_date=date(2022, 6, 30), duration_days=91, value=Decimal("85"), accession_number="0002")

        w24_q1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w24_q2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w22_a, w22_b, w24_q1, w24_q2_ytd])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED

        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_year=2024)
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.left_operand.selected_value == Decimal("220")
        assert elig.right_operand.selected_value == Decimal("100")

    def test_64_target_fiscal_year_isolates_unrelated_ytd_conflict(self):
        """Scenario 15: FY2022 Q2 YTD conflict does not block clean FY2024 derivation."""
        w22_ytd_a = _make_winner_result(
            fiscal_year=2022, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2022, 1, 1), end_date=date(2022, 6, 30), duration_days=181, value=Decimal("150"), accession_number="0001"
        )
        w22_ytd_b = _make_winner_result(
            fiscal_year=2022, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2022, 1, 1), end_date=date(2022, 6, 30), duration_days=181, value=Decimal("155"), accession_number="0002"
        )

        w24_q1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w24_q2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w22_ytd_a, w22_ytd_b, w24_q1, w24_q2_ytd])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED

        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_year=2024)
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.left_operand.selected_value == Decimal("220")
        assert elig.right_operand.selected_value == Decimal("100")

    def test_65_strict_q2_relational_boundary_overlap_fails(self):
        """Scenario 16/17: Standalone Q2 candidate starting on Q1 end date (Mar 31) overlaps by 1 day -> NOT ORIGINAL_AVAILABLE."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        # Overlapping standalone Q2: start is Mar 31 (equal to Q1 end date)
        w2_overlap = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 3, 31), end_date=date(2024, 6, 30), duration_days=92, value=Decimal("120")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w2_overlap])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))

        # Because w2_overlap fails strict start > q1.end check, it cannot be ORIGINAL_AVAILABLE, falls back to ELIGIBLE
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.expected_formula == "Q2_YTD - Q1"

    def test_66_strict_q3_relational_boundary_overlap_fails(self):
        """Scenario 18/19: Standalone Q3 candidate starting on Q2 YTD end date (Jun 30) overlaps by 1 day -> NOT ORIGINAL_AVAILABLE."""
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), value=Decimal("220")
        )
        w3_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q3", start_date=date(2024, 1, 1), end_date=date(2024, 9, 30), value=Decimal("350")
        )
        # Overlapping standalone Q3: start is Jun 30 (equal to Q2 YTD end date)
        w3_overlap = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q3", start_date=date(2024, 6, 30), end_date=date(2024, 9, 30), value=Decimal("130")
        )

        series = SECFiscalSeriesAssembler.assemble_series([w2_ytd, w3_ytd, w3_overlap])[0]
        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q3", target_fiscal_start_date=date(2024, 1, 1))

        # Because w3_overlap fails strict start > q2_ytd.end check, it cannot be ORIGINAL_AVAILABLE, falls back to ELIGIBLE
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.expected_formula == "Q3_YTD - Q2_YTD"

    def test_67_shared_predicate_point_and_conflict_consistency(self):
        """Scenario 21/22/23/34: Point and conflict interval classification predicates produce identical decisions."""
        w1_point = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd_point = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        series = SECFiscalSeriesAssembler.assemble_series([w1_point, w2_ytd_point])[0]
        q1_anchor = series.points[0]
        q2_ytd_anchor = series.points[1]

        # Valid standalone Q2 (Apr 1 - Jun 30)
        res_valid = _check_standalone_q2_interval(
            SECEconomicPeriodKind.QUARTER_DURATION, date(2024, 4, 1), date(2024, 6, 30), 91, "Q2",
            date(2024, 1, 1), q1_anchor, q2_ytd_anchor
        )
        assert res_valid == SECIntervalMatchResult.MATCH

        # Invalid overlapping standalone Q2 (Mar 31 - Jun 30)
        res_overlap = _check_standalone_q2_interval(
            SECEconomicPeriodKind.QUARTER_DURATION, date(2024, 3, 31), date(2024, 6, 30), 92, "Q2",
            date(2024, 1, 1), q1_anchor, q2_ytd_anchor
        )
        assert res_overlap == SECIntervalMatchResult.NO_MATCH

        # Contradicting fp standalone Q2 (Apr 1 - Jun 30 with fp="Q1")
        res_contra = _check_standalone_q2_interval(
            SECEconomicPeriodKind.QUARTER_DURATION, date(2024, 4, 1), date(2024, 6, 30), 91, "Q1",
            date(2024, 1, 1), q1_anchor, q2_ytd_anchor
        )
        assert res_contra == SECIntervalMatchResult.CONTRADICTION

    def test_68_overlapping_conflict_does_not_block_valid_ytd_derivation(self):
        """Scenario 35: Overlapping interval conflict (Mar 31-Jun 30) is not standalone Q2, so it does not block derivation."""
        w1 = _make_winner_result(fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w2_ytd = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )
        # Conflicting facts on overlapping interval (Mar 31 to Jun 30)
        w_overlap_a = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 3, 31), end_date=date(2024, 6, 30), duration_days=92, value=Decimal("120"), accession_number="0001"
        )
        w_overlap_b = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 3, 31), end_date=date(2024, 6, 30), duration_days=92, value=Decimal("125"), accession_number="0002"
        )

        series = SECFiscalSeriesAssembler.assemble_series([w1, w2_ytd, w_overlap_a, w_overlap_b])[0]
        assert series.status == SECFiscalSeriesStatus.CONFLICTED

        elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        # The overlapping conflict is NOT standalone Q2 (fails start > q1.end), so clean derivation proceeds!
        assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
        assert elig.expected_formula == "Q2_YTD - Q1"

    def test_69_fp_contradictions_fail_closed(self):
        """Scenario 37: Conflicting fp metadata on strongly proven intervals returns PERIOD_IDENTITY_UNRESOLVED."""
        # Jan-Mar point with fp="Q2" -> evaluating Q1 returns PERIOD_IDENTITY_UNRESOLVED
        w_q1_bad_fp = _make_winner_result(fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        s_q1 = SECFiscalSeriesAssembler.assemble_series([w_q1_bad_fp])[0]
        elig_q1 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_q1, "Q1", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_q1.status == SECDerivationEligibilityStatus.PERIOD_IDENTITY_UNRESOLVED

        # Apr-Jun point with fp="Q1" -> evaluating Q2 returns PERIOD_IDENTITY_UNRESOLVED
        w_q2_bad_fp = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q1", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), duration_days=91, value=Decimal("120")
        )
        s_q2 = SECFiscalSeriesAssembler.assemble_series([w_q2_bad_fp])[0]
        elig_q2 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_q2, "Q2", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_q2.status == SECDerivationEligibilityStatus.PERIOD_IDENTITY_UNRESOLVED

        # Jul-Sep point with fp="Q2" -> evaluating Q3 returns PERIOD_IDENTITY_UNRESOLVED
        w_q3_bad_fp = _make_winner_result(
            economic_period_kind=SECEconomicPeriodKind.QUARTER_DURATION,
            fiscal_period="Q2", start_date=date(2024, 7, 1), end_date=date(2024, 9, 30), duration_days=92, value=Decimal("130")
        )
        s_q3 = SECFiscalSeriesAssembler.assemble_series([w_q3_bad_fp])[0]
        elig_q3 = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(s_q3, "Q3", target_fiscal_start_date=date(2024, 1, 1))
        assert elig_q3.status == SECDerivationEligibilityStatus.PERIOD_IDENTITY_UNRESOLVED

    def test_70_order_independence_with_conflicts_and_points(self):
        """Scenario 40: Permuting input order with both points and conflicts maintains exact status and outcome."""
        w22_a = _make_winner_result(fiscal_year=2022, fiscal_period="Q2", start_date=date(2022, 4, 1), end_date=date(2022, 6, 30), duration_days=91, value=Decimal("80"), accession_number="0001")
        w22_b = _make_winner_result(fiscal_year=2022, fiscal_period="Q2", start_date=date(2022, 4, 1), end_date=date(2022, 6, 30), duration_days=91, value=Decimal("85"), accession_number="0002")
        w24_q1 = _make_winner_result(fiscal_year=2024, fiscal_period="Q1", start_date=date(2024, 1, 1), end_date=date(2024, 3, 31), duration_days=91, value=Decimal("100"))
        w24_q2_ytd = _make_winner_result(
            fiscal_year=2024, economic_period_kind=SECEconomicPeriodKind.YTD_DURATION,
            fiscal_period="Q2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 30), duration_days=182, value=Decimal("220")
        )

        items = [w22_a, w22_b, w24_q1, w24_q2_ytd]
        for perm in itertools.permutations(items):
            series = SECFiscalSeriesAssembler.assemble_series(list(perm))[0]
            assert series.status == SECFiscalSeriesStatus.CONFLICTED
            elig = SECFiscalSeriesEvaluator.evaluate_quarter_derivation_eligibility(series, "Q2", target_fiscal_year=2024)
            assert elig.status == SECDerivationEligibilityStatus.ELIGIBLE
            assert elig.left_operand.selected_value == Decimal("220")
            assert elig.right_operand.selected_value == Decimal("100")



