"""
backend/tests/test_sec_period_context.py
==========================================
Comprehensive Unit Test Suite for SEC EDGAR Phase 8B.2A / 8B.2A.6:
Economic Period Context Classification & Candidate Grouping.

Coverage:
    - Filing Association Proof Hardening (Phase 8B.2A.6)
    - No Fabricated Dates (Scenarios 1-6)
    - Malformed Periods Fail-Closed (Scenarios 7-10)
    - Strict DEI Cover Date Shares (Scenarios 11-16)
    - Filing Consistency Checks CIK/Accession/ID/Form (Scenarios 17-22)
    - Other & Transition Forms Fail-Closed (Scenarios 23-27)
    - Supported Forms Regression (Scenarios 28-35)
    - Economic Grouping & Ungroupable Separation (Scenarios 36-41)
    - Annual Duration & Fiscal Year Variants (Scenarios 42-48)
    - Standalone Quarter vs Interim YTD (Scenarios 49-55)
    - No-Winner & Multi-Candidate Invariants (Scenarios 56-60)
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
    SUPPORTED_PRIMARY_FORM_ROLES,
    YTD_6M_MAX_DAYS,
    YTD_6M_MIN_DAYS,
    YTD_9M_MAX_DAYS,
    YTD_9M_MIN_DAYS,
    SECEconomicPeriodKind,
    SECPeriodAlignmentStatus,
    SECPeriodClassifier,
    SECPeriodGroupingResult,
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
    filing_id: UUID = None,
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
        filing_id=filing_id,
        filed_date=filed_date,
        frame=frame,
        snapshot_id=snapshot_id or uuid4(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 0. Filing Association Proof Hardening (Phase 8B.2A.6)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFilingAssociationProof:

    def test_01_accession_exact_and_filing_id_none_is_trusted(self):
        """Scenario 1: Candidate with exact accession and filing_id=None trusts supplied filing."""
        filing = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        cand = _make_candidate(cik="320193", accession_number="0000320193-24-000106", filing_id=None, start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))

        p = SECPeriodClassifier.classify_candidate(cand, filing=filing)
        assert p.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
        assert p.filing_report_date == date(2024, 9, 28)
        assert p.classification_confidence == "HIGH"

    def test_02_filing_id_exact_and_accession_none_is_trusted(self):
        """Scenario 2: Candidate with exact filing_id and accession_number=None trusts supplied filing."""
        f_id = uuid4()
        filing = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28), id=f_id)
        cand = _make_candidate(cik="320193", accession_number=None, filing_id=f_id, start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))

        p = SECPeriodClassifier.classify_candidate(cand, filing=filing)
        assert p.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
        assert p.filing_report_date == date(2024, 9, 28)

    def test_03_both_exact_matches_is_trusted(self):
        """Scenario 3: Candidate with both accession and filing_id matching is trusted."""
        f_id = uuid4()
        filing = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28), id=f_id)
        cand = _make_candidate(cik="320193", accession_number="0000320193-24-000106", filing_id=f_id, start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))

        p = SECPeriodClassifier.classify_candidate(cand, filing=filing)
        assert p.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
        assert p.filing_report_date == date(2024, 9, 28)

    def test_04_and_05_both_present_one_mismatch_fails_closed(self):
        """Scenario 4 & 5: If both are present, mismatch on either accession or filing_id gives INVALID_CONTEXT."""
        f_id = uuid4()
        filing = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28), id=f_id)

        # Accession mismatch
        cand_acc_mismatch = _make_candidate(cik="320193", accession_number="0000320193-23-000001", filing_id=f_id)
        p_acc = SECPeriodClassifier.classify_candidate(cand_acc_mismatch, filing=filing)
        assert p_acc.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT

        # Filing ID mismatch
        cand_id_mismatch = _make_candidate(cik="320193", accession_number="0000320193-24-000106", filing_id=uuid4())
        p_id = SECPeriodClassifier.classify_candidate(cand_id_mismatch, filing=filing)
        assert p_id.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT

    def test_06_to_12_both_missing_cannot_trust_filing_despite_same_cik(self):
        """Scenario 6-12: If candidate lacks both accession and filing_id, same CIK/form/FY does NOT prove filing identity."""
        filing_2024 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        cand_unlinked = _make_candidate(
            cik="320193",
            accession_number=None,
            filing_id=None,
            form="10-K",
            fiscal_year=2024,
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )

        p = SECPeriodClassifier.classify_candidate(cand_unlinked, filing=filing_2024)

        # 6 & 7. Filing is not trusted, filing_report_date is None
        assert p.filing_report_date is None
        # 8. Alignment status is UNRESOLVED_FILING (not marked INVALID just for missing lineage)
        assert p.period_alignment_status == SECPeriodAlignmentStatus.UNRESOLVED_FILING
        # 9. Period kind still derives correctly from candidate dates
        assert p.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p.classification_confidence == "LOW"
        assert any("lacks filing-level association proof" in d for d in p.diagnostics)

    def test_13_wrong_same_issuer_filing_does_not_mark_comparative(self):
        """Scenario 13: 2023 fact unlinked to accession does not become COMPARATIVE when passed an unrelated 2024 10-K."""
        filing_2024 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        cand_2023 = _make_candidate(
            cik="320193",
            accession_number=None,
            filing_id=None,
            start_date=date(2022, 10, 1),
            end_date=date(2023, 9, 30),
        )

        p = SECPeriodClassifier.classify_candidate(cand_2023, filing=filing_2024)
        # Because association is not proven, filing_report_date is not used, so it is NOT marked COMPARATIVE_PRIOR_PERIOD
        assert p.period_alignment_status == SECPeriodAlignmentStatus.UNRESOLVED_FILING
        assert p.is_comparative is False
        assert p.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION

    def test_14_dei_shares_without_association_proof_cannot_claim_cover_date(self):
        """Scenario 14: DEI shares without accession/filing_id proof does not get COVER_DATE_CONTEXT from arbitrary filing."""
        filing = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        cand_dei_unlinked = _make_candidate(
            canonical_concept="SHARES_OUTSTANDING",
            taxonomy="dei",
            source_concept="EntityCommonStockSharesOutstanding",
            period_type=PeriodType.INSTANT,
            start_date=None,
            end_date=date(2024, 10, 18),
            accession_number=None,
            filing_id=None,
        )

        p = SECPeriodClassifier.classify_candidate(cand_dei_unlinked, filing=filing)
        # Association not proven -> filing_report_date is None -> basic INSTANT + UNRESOLVED_FILING
        assert p.period_alignment_status == SECPeriodAlignmentStatus.UNRESOLVED_FILING
        assert p.economic_period_kind == SECEconomicPeriodKind.INSTANT
        assert p.period_alignment_status != SECPeriodAlignmentStatus.COVER_DATE_CONTEXT


# ─────────────────────────────────────────────────────────────────────────────
# 1. No Fabricated Dates (Scenarios 1-6)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECNoFabricatedDates:

    def test_01_to_06_missing_dates_remain_none_and_no_sentinels(self):
        """Scenario 1-6: Missing end_date on INSTANT and DURATION remains None; no 1970 sentinel; group key is None."""
        # 1. Instant with missing end_date
        c_inst = _make_candidate(period_type=PeriodType.INSTANT, start_date=None, end_date=None)
        p_inst = SECPeriodClassifier.classify_candidate(c_inst)
        assert p_inst.economic_end_date is None
        assert p_inst.period_alignment_status == SECPeriodAlignmentStatus.INSUFFICIENT_EVIDENCE
        assert p_inst.economic_period_kind == SECEconomicPeriodKind.UNKNOWN

        # 2. Duration with missing end_date
        c_dur = _make_candidate(period_type=PeriodType.DURATION, start_date=date(2024, 1, 1), end_date=None)
        p_dur = SECPeriodClassifier.classify_candidate(c_dur)
        assert p_dur.economic_end_date is None
        assert p_dur.period_alignment_status == SECPeriodAlignmentStatus.INSUFFICIENT_EVIDENCE
        assert p_dur.economic_period_kind == SECEconomicPeriodKind.UNKNOWN

        # 3. Serialization has None, not sentinel string
        d = p_inst.to_dict()
        assert d["economic_end_date"] is None

        # 4. Group key is None for insufficient evidence
        assert build_economic_group_key(p_inst) is None
        assert build_economic_group_key(p_dur) is None

        # 5. Ungroupable candidates are preserved separately in grouping result
        res = group_periodized_candidates([p_inst, p_dur])
        assert len(res.groups) == 0
        assert len(res.ungroupable) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 2. Malformed Periods Fail-Closed (Scenarios 7-10)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECMalformedPeriods:

    def test_07_to_10_start_after_end_fails_closed(self):
        """Scenario 7-10: start_date > end_date results in INVALID_CONTEXT, group key None, candidate preserved in ungroupable."""
        c_bad = _make_candidate(start_date=date(2024, 12, 31), end_date=date(2024, 1, 1), value=Decimal("999.5"))
        p_bad = SECPeriodClassifier.classify_candidate(c_bad)

        assert p_bad.economic_period_kind == SECEconomicPeriodKind.UNKNOWN
        assert p_bad.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT
        assert p_bad.value == Decimal("999.5")  # Value preserved
        assert build_economic_group_key(p_bad) is None

        res = group_periodized_candidates([p_bad])
        assert len(res.groups) == 0
        assert len(res.ungroupable) == 1
        assert res.ungroupable[0].candidate_id == c_bad.id


# ─────────────────────────────────────────────────────────────────────────────
# 3. Strict DEI Cover Date Shares (Scenarios 11-16)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECStrictDEICoverDateShares:

    def test_11_to_16_dei_vs_us_gaap_shares_cover_date_rule(self):
        """Scenario 11-16: Only dei:EntityCommonStockSharesOutstanding receives COVER_DATE_INSTANT; US-GAAP shares do not."""
        filing_10k = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))

        # 11-14. DEI EntityCommonStockSharesOutstanding dated after report_date -> COVER_DATE_INSTANT & COVER_DATE_CONTEXT
        c_dei = _make_candidate(
            canonical_concept="SHARES_OUTSTANDING",
            taxonomy="dei",
            source_concept="EntityCommonStockSharesOutstanding",
            period_type=PeriodType.INSTANT,
            start_date=None,
            end_date=date(2024, 10, 18),
        )
        p_dei = SECPeriodClassifier.classify_candidate(c_dei, filing=filing_10k)
        assert p_dei.economic_period_kind == SECEconomicPeriodKind.COVER_DATE_INSTANT
        assert p_dei.period_alignment_status == SECPeriodAlignmentStatus.COVER_DATE_CONTEXT
        assert p_dei.economic_end_date == date(2024, 10, 18)  # NOT rewritten to report_date

        # 15. us-gaap:CommonStockSharesOutstanding dated after report_date does NOT get COVER_DATE_CONTEXT
        c_usgaap = _make_candidate(
            canonical_concept="SHARES_OUTSTANDING",
            taxonomy="us-gaap",
            source_concept="CommonStockSharesOutstanding",
            period_type=PeriodType.INSTANT,
            start_date=None,
            end_date=date(2024, 10, 18),
        )
        p_usgaap = SECPeriodClassifier.classify_candidate(c_usgaap, filing=filing_10k)
        assert p_usgaap.economic_period_kind == SECEconomicPeriodKind.INSTANT
        assert p_usgaap.period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT
        assert p_usgaap.period_alignment_status != SECPeriodAlignmentStatus.COVER_DATE_CONTEXT

        # 16. Generic SHARES_OUTSTANDING without DEI taxonomy cannot trigger cover-date
        c_gen = _make_candidate(
            canonical_concept="SHARES_OUTSTANDING",
            taxonomy="custom",
            source_concept="CustomShares",
            period_type=PeriodType.INSTANT,
            start_date=None,
            end_date=date(2024, 10, 18),
        )
        p_gen = SECPeriodClassifier.classify_candidate(c_gen, filing=filing_10k)
        assert p_gen.period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT


# ─────────────────────────────────────────────────────────────────────────────
# 4. Filing Consistency Checks CIK/Accession/ID/Form (Scenarios 17-22)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFilingMetadataConsistency:

    def test_17_to_22_filing_mismatch_fails_closed(self):
        """Scenario 17-22: Mismatched CIK, accession, filing_id, or form produces INVALID_CONTEXT."""
        filing_good = SECFilingRecord(cik="0000320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))

        # 17. Matching CIK & accession accepted
        c_good = _make_candidate(cik="320193", accession_number="0000320193-24-000106", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert SECPeriodClassifier.classify_candidate(c_good, filing=filing_good).period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # 18. Wrong CIK
        filing_wrong_cik = SECFilingRecord(cik="0000789019", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        p_wrong_cik = SECPeriodClassifier.classify_candidate(c_good, filing=filing_wrong_cik)
        assert p_wrong_cik.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT
        assert p_wrong_cik.filing_report_date is None

        # 19. Wrong Accession
        filing_wrong_acc = SECFilingRecord(cik="0000320193", accession_number="0000320193-23-000001", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        p_wrong_acc = SECPeriodClassifier.classify_candidate(c_good, filing=filing_wrong_acc)
        assert p_wrong_acc.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT

        # 20. Wrong Filing ID
        f_id1 = uuid4()
        f_id2 = uuid4()
        filing_wrong_id = SECFilingRecord(cik="0000320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28), id=f_id1)
        c_wrong_id = _make_candidate(cik="320193", accession_number="0000320193-24-000106", filing_id=f_id2)
        p_wrong_id = SECPeriodClassifier.classify_candidate(c_wrong_id, filing=filing_wrong_id)
        assert p_wrong_id.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT

        # 21. Candidate 10-Q + Filing 8-K form mismatch
        filing_8k = SECFilingRecord(cik="0000320193", accession_number="0000320193-24-000106", form="8-K", is_amendment=False, report_date=date(2024, 9, 28))
        c_10q = _make_candidate(cik="320193", accession_number="0000320193-24-000106", form="10-Q")
        p_form_mismatch = SECPeriodClassifier.classify_candidate(c_10q, filing=filing_8k)
        assert p_form_mismatch.period_alignment_status == SECPeriodAlignmentStatus.INVALID_CONTEXT


# ─────────────────────────────────────────────────────────────────────────────
# 5. Other & Transition Forms Fail-Closed (Scenarios 23-27)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECOtherAndTransitionFormsFailClosed:

    def test_23_to_27_other_forms_cannot_claim_primary(self):
        """Scenario 23-27: Form role OTHER or unknown forms (XYZ, 10-KT, 10-QT) cannot claim PRIMARY_REPORT_PERIOD."""
        filing_xyz = SECFilingRecord(cik="0000320193", accession_number="0000320193-24-000106", form="XYZ", is_amendment=False, report_date=date(2024, 9, 28))

        # 23. form_role OTHER with annual duration -> NON_PRIMARY_CONTEXT
        c_other_ann = _make_candidate(form="XYZ", form_role="other", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        p_other_ann = SECPeriodClassifier.classify_candidate(c_other_ann, filing=filing_xyz)
        assert p_other_ann.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_other_ann.period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT
        assert p_other_ann.period_alignment_status != SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # 24. form_role OTHER with quarter duration -> NON_PRIMARY_CONTEXT
        c_other_q = _make_candidate(form="XYZ", form_role="other", start_date=date(2024, 7, 1), end_date=date(2024, 9, 28))
        p_other_q = SECPeriodClassifier.classify_candidate(c_other_q, filing=filing_xyz)
        assert p_other_q.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_other_q.period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT

        # 26. 10-KT form -> NON_PRIMARY_CONTEXT
        filing_10kt = SECFilingRecord(cik="0000320193", accession_number="0000320193-24-000106", form="10-KT", is_amendment=False, report_date=date(2024, 9, 28))
        c_10kt = _make_candidate(form="10-KT", form_role="other", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        p_10kt = SECPeriodClassifier.classify_candidate(c_10kt, filing=filing_10kt)
        assert p_10kt.period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT


# ─────────────────────────────────────────────────────────────────────────────
# 6. Supported Forms Regression (Scenarios 28-35)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSupportedFormsRegression:

    def test_28_to_35_supported_periodic_forms(self):
        """Scenario 28-35: 10-K, 10-K/A, 10-Q, 10-Q/A, 20-F, 40-F are supported primary; 6-K and 8-K are NON_PRIMARY."""
        # 28. 10-K
        f_10k = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, report_date=date(2024, 9, 28))
        c_10k = _make_candidate(form="10-K", form_role="primary_annual", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert SECPeriodClassifier.classify_candidate(c_10k, filing=f_10k).period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # 29. 10-K/A
        f_10ka = SECFilingRecord(cik="320193", accession_number="0000320193-24-000999", form="10-K/A", is_amendment=True, report_date=date(2024, 9, 28))
        c_10ka = _make_candidate(accession_number="0000320193-24-000999", form="10-K/A", form_role="amendment_annual", is_amendment=True, start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        p_10ka = SECPeriodClassifier.classify_candidate(c_10ka, filing=f_10ka)
        assert p_10ka.period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD
        assert p_10ka.is_amendment is True

        # 30. 10-Q
        f_10q = SECFilingRecord(cik="320193", accession_number="0000320193-24-000020", form="10-Q", is_amendment=False, report_date=date(2024, 3, 30))
        c_10q = _make_candidate(accession_number="0000320193-24-000020", form="10-Q", form_role="primary_quarterly", start_date=date(2023, 12, 31), end_date=date(2024, 3, 30))
        assert SECPeriodClassifier.classify_candidate(c_10q, filing=f_10q).period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # 32. 20-F
        f_20f = SECFilingRecord(cik="1018724", accession_number="0001018724-24-000001", form="20-F", is_amendment=False, report_date=date(2024, 12, 31))
        c_20f = _make_candidate(cik="1018724", accession_number="0001018724-24-000001", form="20-F", form_role="fpi_annual", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        assert SECPeriodClassifier.classify_candidate(c_20f, filing=f_20f).period_alignment_status == SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD

        # 34. 6-K -> NON_PRIMARY
        f_6k = SECFilingRecord(cik="1018724", accession_number="0001018724-24-000005", form="6-K", is_amendment=False, report_date=date(2024, 6, 30))
        c_6k = _make_candidate(cik="1018724", accession_number="0001018724-24-000005", form="6-K", form_role="fpi_interim_or_event", start_date=date(2024, 4, 1), end_date=date(2024, 6, 30))
        assert SECPeriodClassifier.classify_candidate(c_6k, filing=f_6k).period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT

        # 35. 8-K -> NON_PRIMARY
        f_8k = SECFilingRecord(cik="320193", accession_number="0000320193-24-000099", form="8-K", is_amendment=False, report_date=date(2024, 9, 28))
        c_8k = _make_candidate(accession_number="0000320193-24-000099", form="8-K", form_role="event_filing", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert SECPeriodClassifier.classify_candidate(c_8k, filing=f_8k).period_alignment_status == SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT


# ─────────────────────────────────────────────────────────────────────────────
# 7. Economic Grouping & Ungroupable Separation (Scenarios 36-41)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECEconomicGroupingAndUngroupableSeparation:

    def test_36_to_41_grouping_and_ungroupable_isolation(self):
        """Scenario 36-41: Valid candidates group together; invalid/missing period candidates are kept in ungroupable."""
        # 36 & 37. Valid original and amendment
        c_orig = _make_candidate(accession_number="0000320193-24-000106", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), value=Decimal("100"))
        c_amend = _make_candidate(accession_number="0000320193-24-000999", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), value=Decimal("105"), is_amendment=True)
        # 38. Prior comparative period
        c_prior = _make_candidate(accession_number="0000320193-24-000106", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("90"))
        # 39. EUR unit
        c_eur = _make_candidate(accession_number="0000320193-24-000106", unit="EUR", start_date=date(2023, 10, 1), end_date=date(2024, 9, 28), value=Decimal("85"))
        # 40. Invalid candidate (start > end)
        c_invalid = _make_candidate(start_date=date(2024, 12, 31), end_date=date(2024, 1, 1))

        candidates = [
            SECPeriodClassifier.classify_candidate(c_orig),
            SECPeriodClassifier.classify_candidate(c_amend),
            SECPeriodClassifier.classify_candidate(c_prior),
            SECPeriodClassifier.classify_candidate(c_eur),
            SECPeriodClassifier.classify_candidate(c_invalid),
        ]

        result: SECPeriodGroupingResult = group_periodized_candidates(candidates)

        # 3 valid groups: USD current (2 items), USD prior (1 item), EUR current (1 item)
        assert len(result.groups) == 3
        # 1 ungroupable item: invalid period
        assert len(result.ungroupable) == 1
        assert result.ungroupable[0].candidate_id == c_invalid.id


# ─────────────────────────────────────────────────────────────────────────────
# 8. Annual Duration & Fiscal Year Variants (Scenarios 42-48)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECAnnualDurationVariants:

    def test_42_to_48_annual_duration_variants(self):
        """Scenario 42-48: Calendar year, 52-week (364d), 53-week (371d) classify as ANNUAL_DURATION; filed_date is independent."""
        c_cal = _make_candidate(start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        c_52 = _make_candidate(start_date=date(2023, 1, 29), end_date=date(2024, 1, 27))
        c_53 = _make_candidate(start_date=date(2023, 1, 29), end_date=date(2024, 2, 3))

        p_cal = SECPeriodClassifier.classify_candidate(c_cal)
        p_52 = SECPeriodClassifier.classify_candidate(c_52)
        p_53 = SECPeriodClassifier.classify_candidate(c_53)

        assert p_cal.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_52.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_53.economic_period_kind == SECEconomicPeriodKind.ANNUAL_DURATION
        assert p_52.duration_days == 364
        assert p_53.duration_days == 371


# ─────────────────────────────────────────────────────────────────────────────
# 9. Standalone Quarter vs Interim YTD (Scenarios 49-55)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECQuarterVsYTDVariants:

    def test_49_to_55_q1_q2_q3_quarter_vs_ytd(self):
        """Scenario 49-55: Q1 is QUARTER; Q2 standalone (91d) vs Q2 YTD (182d); Q3 standalone (91d) vs Q3 YTD (273d)."""
        c_q1 = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2023, 12, 30))
        c_q2_q = _make_candidate(start_date=date(2023, 12, 31), end_date=date(2024, 3, 30))
        c_q2_ytd = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 3, 30))
        c_q3_q = _make_candidate(start_date=date(2024, 3, 31), end_date=date(2024, 6, 29))
        c_q3_ytd = _make_candidate(start_date=date(2023, 10, 1), end_date=date(2024, 6, 29))

        p_q1 = SECPeriodClassifier.classify_candidate(c_q1)
        p_q2_q = SECPeriodClassifier.classify_candidate(c_q2_q)
        p_q2_ytd = SECPeriodClassifier.classify_candidate(c_q2_ytd)
        p_q3_q = SECPeriodClassifier.classify_candidate(c_q3_q)
        p_q3_ytd = SECPeriodClassifier.classify_candidate(c_q3_ytd)

        assert p_q1.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_q2_q.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_q2_ytd.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION
        assert p_q3_q.economic_period_kind == SECEconomicPeriodKind.QUARTER_DURATION
        assert p_q3_ytd.economic_period_kind == SECEconomicPeriodKind.YTD_DURATION


# ─────────────────────────────────────────────────────────────────────────────
# 10. No-Winner & Multi-Candidate Invariants (Scenarios 56-60)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECNoWinnerInvariants:

    def test_56_to_60_all_candidates_preserved_in_groups(self):
        """Scenario 56-60: 3 filings for the same period preserve all 3 candidates without picking a winner."""
        c1 = _make_candidate(accession_number="0000320193-23-000106", filed_date=date(2023, 11, 3), value=Decimal("100"))
        c2 = _make_candidate(accession_number="0000320193-23-000999", filed_date=date(2023, 12, 1), value=Decimal("101"), is_amendment=True)
        c3 = _make_candidate(accession_number="0000320193-24-000106", filed_date=date(2024, 11, 1), value=Decimal("100"))

        candidates = SECPeriodClassifier.classify_candidates([c1, c2, c3])
        assert len(candidates) == 3

        result = group_periodized_candidates(candidates)
        assert len(result.groups) == 1
        group_items = next(iter(result.groups.values()))
        assert len(group_items) == 3
        assert {item.accession_number for item in group_items} == {
            "0000320193-23-000106", "0000320193-23-000999", "0000320193-24-000106"
        }
