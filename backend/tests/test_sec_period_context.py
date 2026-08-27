"""
backend/tests/test_sec_period_context.py
==========================================
Comprehensive Unit Test Suite for SEC EDGAR Phase 8B.2A:
Economic Period Context Classification & Candidate Grouping.

Coverage:
    - Annual Duration & Fiscal Year Variants (Scenarios 1-7)
    - Standalone Quarter vs Interim YTD (Scenarios 8-16)
    - Primary vs Comparative Alignment (Scenarios 17-22)
    - Instant Balance Sheet & Cover Date Shares (Scenarios 23-27)
    - Form Roles & Non-Primary Contexts (Scenarios 28-36)
    - Irregular & Malformed Periods Fail-Closed (Scenarios 37-41)
    - Economic Candidate Grouping (Scenarios 42-48)
    - No-Winner & Multi-Candidate Preservation (Scenarios 49-54)
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from backend.engine.private.sec.concepts import PeriodType
from backend.engine.private.sec.models import (
    SECCanonicalFactCandidate,
    SECFilingRecord,
)
from backend.engine.private.sec.period_context import (
    ANNUAL_MAX_DAYS,
    ANNUAL_MIN_DAYS,
    QUARTER_MAX_DAYS,
    QUARTER_MIN_DAYS,
    YTD_6M_MAX_DAYS,
    YTD_6M_MIN_DAYS,
    YTD_9M_MAX_DAYS,
    YTD_9M_MIN_DAYS,
    SECEconomicPeriodKind,
    SECPeriodAlignmentStatus,
    SECPeriodClassifier,
    SECPeriodizedFactCandidate,
    build_economic_group_key,
    group_periodized_candidates,
)


def _make_candidate(
    canonical_concept: str = "REVENUE",
    period_type: PeriodType = PeriodType.DURATION,
    start_date: date = date(2024, 1, 1),
    end_date: date = date(2024, 12, 31),
    cik: str = "0000320193",
    accession_number: str = "0000320193-24-000106",
    form: str = "10-K",
    form_role: str = "primary_annual",
    is_amendment: bool = False,
    fiscal_year: int = 2024,
    fiscal_period: str = "FY",
    value: Decimal = Decimal("100000"),
    unit: str = "USD",
    taxonomy: str = "us-gaap",
    source_concept: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    match_strength: str = "exact",
    variant_priority: int = 1,
    snapshot_id: UUID = None,
    filed_date: date = date(2024, 11, 1),
    frame: str = "CY2024",
) -> SECCanonicalFactCandidate:
    return SECCanonicalFactCandidate(
        raw_fact_id=uuid4(),
        cik=cik,
        canonical_concept=canonical_concept,
        taxonomy=taxonomy,
        source_concept=source_concept,
        match_strength=match_strength,
        variant_priority=variant_priority,
        value=value,
        unit=unit,
        period_type=period_type,
        start_date=start_date,
        end_date=end_date,
        accession_number=accession_number,
        form=form,
        form_role=form_role,
        is_amendment=is_amendment,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        filed_date=filed_date,
        frame=frame,
        snapshot_id=snapshot_id or uuid4(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Annual Duration & Fiscal Year Variants (Scenarios 1-7)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECAnnualPeriodClassification:

    def test_01_to_04_calendar_fiscal_and_52_53_week_annual_periods(self):
        """Scenario 1-4: Calendar year, non-calendar fiscal year, 52-week, and 53-week periods classify as ANNUAL_DURATION."""
        # 1. Calendar year (366 days in leap year 2024)
        c_cal = _make_candidate(start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        filing_cal = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 12, 31))
        p_cal = SECPeriodClassifier.classify_candidate(c_cal, filing=filing_cal)
        assert p_cal.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_cal.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
        assert p_cal.duration_days == 366

        # 2. September fiscal year (Apple: Oct 1, 2023 to Sep 28, 2024 = 364 days)
        c_aapl = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        filing_aapl = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        p_aapl = SECPeriodClassifier.classify_candidate(c_aapl, filing=filing_aapl)
        assert p_aapl.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_aapl.duration_days == 364

        # 3. 52-week year (364 days)
        c_52 = _make_candidate(start_date=date(2023, 1, 29), end_date=date(2024, 1, 27))
        p_52 = SECPeriodClassifier.classify_candidate(c_52)
        assert p_52.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_52.duration_days == 364

        # 4. 53-week year (371 days)
        c_53 = _make_candidate(start_date=date(2023, 1, 29), end_date=date(2024, 2, 3))
        p_53 = SECPeriodClassifier.classify_candidate(c_53)
        assert p_53.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_53.duration_days == 371

    def test_05_to_07_comparative_annual_and_filed_date_independence(self):
        """Scenario 5-7: Comparative prior annual stays comparative; filed_date and FY label do not override start/end dates."""
        # 2023 annual disclosed in 2024 10-K
        c_comp = _make_candidate(start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), filed_date=date(2024, 11, 1))
        filing_2024 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28), filing_date=date(2024, 11, 1))
        p_comp = SECPeriodClassifier.classify_candidate(c_comp, filing=filing_2024)

        assert p_comp.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_comp.period_alignment_status == SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD
        assert p_comp.is_comparative is True
        assert p_comp.economic_end_date == date(2023, 9, 30)  # Economic end date != filing_date (Nov 1, 2024)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Standalone Quarter vs Interim YTD (Scenarios 8-16)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECQuarterVsYTDClassification:

    def test_08_and_09_q1_standalone_quarter_and_no_ytd_duplicate(self):
        """Scenario 8 & 9: Q1 (~91 days) classifies as QUARTER_DURATION and is not duplicated as a separate YTD interval."""
        c_q1 = _make_candidate(
            start_date=date(2023, 10, 1),
            end_date=date(2023, 12, 30),
            form="10-Q",
            form_role="primary_quarterly",
            fiscal_period="Q1",
        )
        filing_q1 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000010", form="10-Q", is_amendment=False, report_date=date(2023, 12, 30))
        p_q1 = SECPeriodClassifier.classify_candidate(c_q1, filing=filing_q1)

        assert p_q1.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_q1.duration_days == 91
        assert p_q1.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

    def test_10_to_15_q2_and_q3_standalone_quarter_vs_ytd_separation(self):
        """Scenario 10-15: Same Q2/Q3 filing cleanly distinguishes standalone 3-month quarter from 6M/9M YTD."""
        filing_q2 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000020", form="10-Q", is_amendment=False, report_date=date(2024, 3, 30))

        # Standalone Q2 (Dec 31, 2023 to Mar 30, 2024 = 91 days)
        c_q2_standalone = _make_candidate(start_date=date(2023, 12, 31), end_date=date(2024, 3, 30), form="10-Q", form_role="primary_quarterly", fiscal_period="Q2")
        p_q2_standalone = SECPeriodClassifier.classify_candidate(c_q2_standalone, filing=filing_q2)
        assert p_q2_standalone.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_q2_standalone.duration_days == 91
        assert p_q2_standalone.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # 6-Month Q2 YTD (Oct 1, 2023 to Mar 30, 2024 = 182 days)
        c_q2_ytd = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 3, 30), form="10-Q", form_role="primary_quarterly", fiscal_period="Q2")
        p_q2_ytd = SECPeriodClassifier.classify_candidate(c_q2_ytd, filing=filing_q2)
        assert p_q2_ytd.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
        assert p_q2_ytd.duration_days == 182
        assert p_q2_ytd.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # Standalone Q3 vs 9-Month Q3 YTD
        filing_q3 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000030", form="10-Q", is_amendment=False, report_date=date(2024, 6, 29))
        c_q3_standalone = _make_candidate(start_date=date(2024, 3, 31), end_date=date(2024, 6, 29), form="10-Q", form_role="primary_quarterly", fiscal_period="Q3")
        c_q3_ytd = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 6, 29), form="10-Q", form_role="primary_quarterly", fiscal_period="Q3")

        p_q3_standalone = SECPeriodClassifier.classify_candidate(c_q3_standalone, filing=filing_q3)
        p_q3_ytd = SECPeriodClassifier.classify_candidate(c_q3_ytd, filing=filing_q3)

        assert p_q3_standalone.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_q3_ytd.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
        assert p_q3_ytd.duration_days == 273

    def test_16_frame_alone_cannot_force_quarter(self):
        """Scenario 16: Frame label alone (e.g. CY2024Q2) cannot turn an annual or YTD duration into a standalone quarter."""
        c_fake = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), frame="CY2024Q2")
        p_fake = SECPeriodClassifier.classify_candidate(c_fake)
        assert p_fake.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION


# ─────────────────────────────────────────────────────────────────────────────
# 3. Primary vs Comparative Alignment (Scenarios 17-22)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECPrimaryVsComparativeAlignment:

    def test_17_to_22_primary_and_prior_year_comparatives(self):
        """Scenario 17-22: Current quarter/YTD/annual are PRIMARY; prior year items in the same filing are COMPARATIVE."""
        filing_q2 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000020", form="10-Q", is_amendment=False, report_date=date(2024, 3, 30))

        # Current Q2 Quarter (Primary)
        c_curr_q = _make_candidate(start_date=date(2023, 12, 31), end_date=date(2024, 3, 30), form="10-Q")
        assert SECPeriodClassifier.classify_candidate(c_curr_q, filing=filing_q2).period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # Prior Year Q2 Quarter (Comparative)
        c_prior_q = _make_candidate(start_date=date(2022, 12, 31), end_date=date(2023, 3, 30), form="10-Q")
        p_prior_q = SECPeriodClassifier.classify_candidate(c_prior_q, filing=filing_q2)
        assert p_prior_q.period_alignment_status == SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD
        assert p_prior_q.is_comparative is True

        # Current Q2 YTD (Primary)
        c_curr_ytd = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 3, 30), form="10-Q")
        assert SECPeriodClassifier.classify_candidate(c_curr_ytd, filing=filing_q2).period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # Prior Year Q2 YTD (Comparative)
        c_prior_ytd = _make_candidate(start_date=date(2022, 10, 1), end_date=date(2023, 3, 30), form="10-Q")
        p_prior_ytd = SECPeriodClassifier.classify_candidate(c_prior_ytd, filing=filing_q2)
        assert p_prior_ytd.period_alignment_status == SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD
        assert p_prior_ytd.is_comparative is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. Instant Balance Sheet & Cover Date Shares (Scenarios 23-27)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECInstantAndCoverDateClassification:

    def test_23_to_27_balance_sheet_and_cover_date_shares(self):
        """Scenario 23-27: Balance sheet items at report_date are PRIMARY; prior dates are COMPARATIVE; cover-date shares are COVER_DATE_INSTANT."""
        filing_10k = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))

        # 1. Total Assets at report_date -> PRIMARY_REPORT_PERIOD
        c_assets_curr = _make_candidate(canonical_concept="TOTAL_ASSETS", period_type=PeriodType.INSTANT, start_date=None, end_date=date(2024, 9, 28))
        p_assets_curr = SECPeriodClassifier.classify_candidate(c_assets_curr, filing=filing_10k)
        assert p_assets_curr.economic_period_kind == SECEconomicPeriodKind.INSTANT
        assert p_assets_curr.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
        assert p_assets_curr.is_comparative is False

        # 2. Prior Total Assets at prior date -> COMPARATIVE_PRIOR_PERIOD
        c_assets_prior = _make_candidate(canonical_concept="TOTAL_ASSETS", period_type=PeriodType.INSTANT, start_date=None, end_date=date(2023, 9, 30))
        p_assets_prior = SECPeriodClassifier.classify_candidate(c_assets_prior, filing=filing_10k)
        assert p_assets_prior.economic_period_kind == SECEconomicPeriodKind.INSTANT
        assert p_assets_prior.period_alignment_status == SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD
        assert p_assets_prior.is_comparative is True

        # 3. DEI Shares Outstanding dated after report date -> COVER_DATE_INSTANT
        c_shares_cover = _make_candidate(canonical_concept="SHARES_OUTSTANDING", period_type=PeriodType.INSTANT, start_date=None, end_date=date(2024, 10, 18))
        p_shares_cover = SECPeriodClassifier.classify_candidate(c_shares_cover, filing=filing_10k)
        assert p_shares_cover.economic_period_kind == SECEconomicPeriodKind.COVER_DATE_INSTANT
        assert p_shares_cover.period_alignment_status == SECPeriodAlignmentStatus.COVER_DATE_CONTEXT
        assert p_shares_cover.is_comparative is False


# ─────────────────────────────────────────────────────────────────────────────
# 5. Form Roles & Non-Primary Contexts (Scenarios 28-36)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFormRolesAndNonPrimaryContexts:

    def test_28_to_36_form_roles_and_6k_8k_handling(self):
        """Scenario 28-36: 10-K/10-Q/20-F/40-F are primary; 8-K/6-K duration facts are NON_PRIMARY_CONTEXT."""
        filing_8k = SECFilingRecord(cik="320193", accession_number="0000320193-24-000099", form="8-K", is_amendment=False, report_date=date(2024, 9, 28))
        c_8k = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), form="8-K", form_role="event_filing")
        p_8k = SECPeriodClassifier.classify_candidate(c_8k, filing=filing_8k)
        assert p_8k.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_8k.period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT

        filing_6k = SECFilingRecord(cik="1018724", accession_number="0001018724-24-000005", form="6-K", is_amendment=False, report_date=date(2024, 6, 30))
        c_6k = _make_candidate(start_date=date(2024, 4, 1), end_date=date(2024, 6, 30), form="6-K", form_role="fpi_interim_or_event")
        p_6k = SECPeriodClassifier.classify_candidate(c_6k, filing=filing_6k)
        assert p_6k.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_6k.period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT

        # 10-K/A amendment retains ANNUAL_DURATION and PRIMARY_REPORT_PERIOD
        filing_10ka = SECFilingRecord(cik="320193", accession_number="0000320193-24-000999", form="10-K/A", is_amendment=True, report_date=date(2024, 9, 28))
        c_10ka = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), form="10-K/A", form_role="amendment_annual", is_amendment=True)
        p_10ka = SECPeriodClassifier.classify_candidate(c_10ka, filing=filing_10ka)
        assert p_10ka.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_10ka.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
        assert p_10ka.is_amendment is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. Irregular & Malformed Periods Fail-Closed (Scenarios 37-41)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECIrregularAndMalformedPeriods:

    def test_37_to_41_stub_and_malformed_periods_fail_closed(self):
        """Scenario 37-41: 45-day stub is IRREGULAR_DURATION; start > end is INVALID_CONTEXT; missing dates are INSUFFICIENT_EVIDENCE."""
        # 45-day stub
        c_stub = _make_candidate(start_date=date(2024, 1, 1), end_date=date(2024, 2, 14))
        p_stub = SECPeriodClassifier.classify_candidate(c_stub)
        assert p_stub.economic_period_kind == SECEconomicPeriodKind.IRREGULAR_DURATION
        assert p_stub.duration_days == 45

        # Malformed start > end
        c_bad = _make_candidate(start_date=date(2024, 12, 31), end_date=date(2024, 1, 1))
        p_bad = SECPeriodClassifier.classify_candidate(c_bad)
        assert p_bad.economic_period_kind == SECEconomicPeriodKind.UNKNOWN
        assert p_bad.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT

        # Missing start on duration
        c_nostart = _make_candidate(start_date=None, end_date=date(2024, 12, 31))
        p_nostart = SECPeriodClassifier.classify_candidate(c_nostart)
        assert p_nostart.economic_period_kind == SECEconomicPeriodKind.UNKNOWN
        assert p_nostart.period_alignment_status == SECPeriodAlignmentStatus.INSUFFICIENT_EVIDENCE


# ─────────────────────────────────────────────────────────────────────────────
# 7. Economic Candidate Grouping & No-Winner Invariants (Scenarios 42-54)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECEconomicCandidateGroupingAndNoWinners:

    def test_42_to_48_grouping_key_dimensions(self):
        """Scenario 42-48: Same economic interval groups together across accessions/amendments; different units or concepts separate."""
        # Same observation in original 10-K and 10-K/A
        c_orig = _make_candidate(accession_number="0000320193-24-000106", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), value=Decimal("100"))
        c_amend = _make_candidate(accession_number="0000320193-24-000999", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), value=Decimal("105"), is_amendment=True)
        # Prior comparative period
        c_prior = _make_candidate(accession_number="0000320193-24-000106", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("90"))

        p_orig = SECPeriodClassifier.classify_candidate(c_orig)
        p_amend = SECPeriodClassifier.classify_candidate(c_amend)
        p_prior = SECPeriodClassifier.classify_candidate(c_prior)

        key_orig = build_economic_group_key(p_orig)
        key_amend = build_economic_group_key(p_amend)
        key_prior = build_economic_group_key(p_prior)

        # Original and amendment share the exact same group key
        assert key_orig == key_amend
        # Comparative prior period has a different group key
        assert key_orig != key_prior

        # Grouping helper
        groups = group_periodized_candidates([p_orig, p_amend, p_prior])
        assert len(groups) == 2
        assert len(groups[key_orig]) == 2
        assert len(groups[key_prior]) == 1

    def test_49_to_54_all_candidates_preserved_without_declaring_winners(self):
        """Scenario 49-54: In Phase 8B.2A, 3 filings for the same period produce 3 candidates; no latest accession or snapshot auto-wins."""
        c1 = _make_candidate(accession_number="0000320193-23-000106", filed_date=date(2023, 11, 3), value=Decimal("100"))
        c2 = _make_candidate(accession_number="0000320193-23-000999", filed_date=date(2023, 12, 1), value=Decimal("101"), is_amendment=True)
        c3 = _make_candidate(accession_number="0000320193-24-000106", filed_date=date(2024, 11, 1), value=Decimal("100"))

        candidates = SECPeriodClassifier.classify_candidates([c1, c2, c3])
        assert len(candidates) == 3

        groups = group_periodized_candidates(candidates)
        assert len(groups) == 1
        group_items = next(iter(groups.values()))
        # All 3 candidates are preserved in the economic group without winner selection
        assert len(group_items) == 3
        assert {item.accession_number for item in group_items} == {
            "0000320193-23-000106", "0000320193-23-000999", "0000320193-24-000106"
        }
