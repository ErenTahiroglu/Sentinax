"""
backend/tests/test_sec_winner_resolver.py
===========================================
Comprehensive Test Suite for SEC EDGAR Phase 8B.2B:
Snapshot-Scoped PIT Filing Precedence, Restatement Reconciliation & Winner Resolution.

Tests:
    - Snapshot Selection (1-13)
    - Snapshot Removal & Source Correction (14-18)
    - Candidate Eligibility (19-28)
    - Filing Lineage Resolution (29-35)
    - Disclosure Chronology Comparator (36-42)
    - Same-Filing Semantic Reconciliation (43-47)
    - Amendment Precedence (48-52)
    - Comparative Restatement Precedence (53-57)
    - Cross-Filing Semantic Quality (58-62)
    - Current Snapshot Isolation (Section 77)
    - Group Integrity & Rejections (63-68)
    - Point-In-Time Lookahead Protection (69-73)
    - Result Auditability & Diagnostics (74-82)
"""

import itertools
import pytest
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID, uuid4

from backend.engine.private.storage_models import RawProviderSnapshotRecord, compute_payload_hash
from backend.engine.private.sec.models import (
    SECFilingRecord,
    PeriodType,
)
from backend.engine.private.sec.period_context import (
    SECEconomicPeriodKind,
    SECPeriodAlignmentStatus,
    SECPeriodizedFactCandidate,
    build_economic_group_key,
)
from backend.engine.private.sec.winner_resolver import (
    SECWinnerResolutionMode,
    SECWinnerStatus,
    FilingDisclosureComparison,
    SECWinnerResolutionResult,
    SECWinnerResolver,
    compare_filing_disclosure_order,
    validate_company_facts_snapshot,
    get_semantic_quality_rank,
)


def _make_snapshot(
    cik: str = "320193",
    retrieved_at: Optional[datetime] = None,
    http_status: int = 200,
    provider: str = "SEC_EDGAR",
    endpoint: Optional[str] = None,
    raw_payload: Optional[Dict[str, Any]] = None,
    is_superseded: bool = False,
    snapshot_id: Optional[UUID] = None,
) -> RawProviderSnapshotRecord:
    ret_time = retrieved_at or datetime(2024, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    endp = endpoint or f"/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
    payload = raw_payload or {
        "cik": int(cik) if cik.isdigit() else cik,
        "entityName": "Apple Inc.",
        "facts": {"us-gaap": {"Revenues": {}}},
    }
    snap = RawProviderSnapshotRecord.create(
        provider=provider,
        endpoint=endp,
        request_params={"cik": cik},
        raw_payload=payload,
        http_status=http_status,
        retrieved_at=ret_time,
    )
    if is_superseded:
        snap.is_superseded = True
    if snapshot_id:
        snap.id = snapshot_id
    return snap


def _make_periodized_candidate(
    snapshot_id: UUID,
    raw_fact_id: Optional[UUID] = None,
    cik: str = "320193",
    canonical_concept: str = "REVENUE",
    value: Optional[Decimal] = Decimal("100000.00"),
    unit: str = "USD",
    economic_period_kind: SECEconomicPeriodKind = SECEconomicPeriodKind.ANNUAL_DURATION,
    period_alignment_status: SECPeriodAlignmentStatus = SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD,
    start_date: Optional[date] = date(2023, 10, 1),
    end_date: Optional[date] = date(2024, 9, 28),
    accession_number: Optional[str] = "0000320193-24-000106",
    filing_id: Optional[UUID] = None,
    form: str = "10-K",
    match_strength: str = "EXACT",
    variant_priority: int = 10,
    taxonomy: str = "us-gaap",
    source_concept: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    is_amendment: bool = False,
    is_comparative: bool = False,
    classification_confidence: str = "HIGH",
) -> SECPeriodizedFactCandidate:
    cid = uuid4()
    rf_id = raw_fact_id or uuid4()
    dur = (end_date - start_date).days if (start_date and end_date) else None
    return SECPeriodizedFactCandidate(
        candidate_id=cid,
        raw_fact_id=rf_id,
        cik=cik,
        canonical_concept=canonical_concept,
        economic_period_kind=economic_period_kind,
        period_alignment_status=period_alignment_status,
        economic_start_date=start_date,
        economic_end_date=end_date,
        duration_days=dur,
        fiscal_year=2024,
        fiscal_period="FY",
        filing_id=filing_id,
        accession_number=accession_number,
        form=form,
        form_role="ANNUAL",
        is_amendment=is_amendment,
        filing_report_date=end_date,
        is_comparative=is_comparative,
        classification_confidence=classification_confidence,
        classification_basis="Test Candidate",
        diagnostics=[],
        value=value,
        unit=unit,
        taxonomy=taxonomy,
        source_concept=source_concept,
        match_strength=match_strength,
        variant_priority=variant_priority,
        snapshot_id=snapshot_id,
        filed_date=end_date,
        frame=None,
    )



# ─────────────────────────────────────────────────────────────────────────────
# 1. Snapshot Selection Tests (Scenarios 1-13)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSnapshotSelection:

    def test_01_current_picks_latest_valid_company_facts_snapshot(self):
        """Scenario 1: CURRENT_REPORTED picks latest valid snapshot."""
        s1 = _make_snapshot(retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        s2 = _make_snapshot(retrieved_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        s3 = _make_snapshot(retrieved_at=datetime(2024, 10, 1, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c3 = _make_periodized_candidate(snapshot_id=s3.id, value=Decimal("300"))
        f3 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))

        res = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c3],
            snapshots=[s1, s2, s3],
            filings=[f3],
            mode=SECWinnerResolutionMode.CURRENT_REPORTED,
        )
        assert res.status == SECWinnerStatus.SELECTED
        assert res.evaluation_snapshot_id == s3.id
        assert res.selected_value == Decimal("300")

    def test_02_to_05_invalid_snapshots_ignored(self):
        """Scenarios 2-5: Submissions, wrong CIK, HTTP error, malformed snapshots are ignored."""
        s_sub = _make_snapshot(endpoint="/api/xbrl/submissions/CIK0000320193.json")
        s_wrong_cik = _make_snapshot(cik="0000789019", endpoint="/api/xbrl/companyfacts/CIK0000789019.json", raw_payload={"cik": 789019, "facts": {}})
        s_500 = _make_snapshot(http_status=500)
        s_malformed = _make_snapshot(raw_payload={"not_facts": "bad"})

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        res = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[],
            snapshots=[s_sub, s_wrong_cik, s_500, s_malformed],
            filings=[],
            mode=SECWinnerResolutionMode.CURRENT_REPORTED,
        )
        assert res.status == SECWinnerStatus.NO_VALID_SNAPSHOT

    def test_06_and_07_same_timestamp_snapshots_hash_conflict_detection(self):
        """Scenario 6 & 7: Same timestamp with same hash is ok; different hash gives SNAPSHOT_CONFLICT."""
        t = datetime(2024, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        s1 = _make_snapshot(retrieved_at=t, raw_payload={"cik": 320193, "facts": {"v": 1}})
        s2 = _make_snapshot(retrieved_at=t, raw_payload={"cik": 320193, "facts": {"v": 2}})

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        res = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[],
            snapshots=[s1, s2],
            filings=[],
            mode=SECWinnerResolutionMode.CURRENT_REPORTED,
        )
        assert res.status == SECWinnerStatus.SNAPSHOT_CONFLICT

    def test_08_to_13_system_as_of_and_source_as_of(self):
        """Scenarios 8-13: SYSTEM_AS_OF boundaries, future exclusion, missing prior, naive as_of, SOURCE_AS_OF."""
        s1 = _make_snapshot(retrieved_at=datetime(2024, 3, 1, tzinfo=timezone.utc))
        s2 = _make_snapshot(retrieved_at=datetime(2024, 7, 1, tzinfo=timezone.utc))
        s3 = _make_snapshot(retrieved_at=datetime(2024, 11, 1, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c2 = _make_periodized_candidate(snapshot_id=s2.id, value=Decimal("200"))
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 6, 1))

        # 9 & 10. as_of between s2 and s3 picks s2
        as_of_t2 = datetime(2024, 8, 1, tzinfo=timezone.utc)
        res_t2 = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c2],
            snapshots=[s1, s2, s3],
            filings=[f],
            mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
            as_of=as_of_t2,
        )
        assert res_t2.status == SECWinnerStatus.SELECTED
        assert res_t2.evaluation_snapshot_id == s2.id
        assert res_t2.selected_value == Decimal("200")

        # 11. No prior snapshot before as_of
        res_early = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c2],
            snapshots=[s1, s2, s3],
            filings=[f],
            mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
            as_of=datetime(2023, 1, 1, tzinfo=timezone.utc),
        )
        assert res_early.status == SECWinnerStatus.NO_SNAPSHOT_AS_OF

        # 12. Naive as_of raises ValueError
        with pytest.raises(ValueError, match="timezone-aware"):
            SECWinnerResolver.resolve_winner(
                economic_group_key=group_key,
                candidates=[c2],
                snapshots=[s1, s2, s3],
                filings=[f],
                mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
                as_of=datetime(2024, 8, 1),
            )

        # 13. SOURCE_AS_OF returns UNAVAILABLE_SOURCE_AS_OF
        res_src = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c2],
            snapshots=[s1, s2, s3],
            filings=[f],
            mode=SECWinnerResolutionMode.SOURCE_AS_OF,
        )
        assert res_src.status == SECWinnerStatus.UNAVAILABLE_SOURCE_AS_OF


# ─────────────────────────────────────────────────────────────────────────────
# 2. Snapshot Removal & Source Correction (Scenarios 14-18)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSnapshotRemovalAndCorrection:

    def test_14_fact_removed_in_new_snapshot_not_resurrected_in_current(self):
        """Scenario 14: Fact present in S1 but absent in S2 is NOT resurrected in CURRENT_REPORTED."""
        s1 = _make_snapshot(retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        s2 = _make_snapshot(retrieved_at=datetime(2024, 6, 1, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        # Candidate only belongs to S1
        c1 = _make_periodized_candidate(snapshot_id=s1.id, value=Decimal("100"))
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 1, 1))

        res_curr = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c1],
            snapshots=[s1, s2],
            filings=[f],
            mode=SECWinnerResolutionMode.CURRENT_REPORTED,
        )
        # S2 is chosen as current evaluation snapshot; c1 from S1 is rejected!
        assert res_curr.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE
        assert res_curr.evaluation_snapshot_id == s2.id

    def test_15_to_17_source_correction_pit_evolution(self):
        """Scenarios 15-17: Fact corrected from 100 in S1 to 102 in S2 across time."""
        s1 = _make_snapshot(retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        s2 = _make_snapshot(retrieved_at=datetime(2024, 6, 1, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c1 = _make_periodized_candidate(snapshot_id=s1.id, value=Decimal("100"))
        c2 = _make_periodized_candidate(snapshot_id=s2.id, value=Decimal("102"))
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 1, 1))

        # 15. CURRENT_REPORTED -> 102
        res_curr = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c1, c2],
            snapshots=[s1, s2],
            filings=[f],
            mode=SECWinnerResolutionMode.CURRENT_REPORTED,
        )
        assert res_curr.status == SECWinnerStatus.SELECTED
        assert res_curr.selected_value == Decimal("102")

        # 16. SYSTEM_AS_OF at March 2024 -> 100
        res_pit1 = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c1, c2],
            snapshots=[s1, s2],
            filings=[f],
            mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
            as_of=datetime(2024, 3, 1, tzinfo=timezone.utc),
        )
        assert res_pit1.status == SECWinnerStatus.SELECTED
        assert res_pit1.selected_value == Decimal("100")

        # 17. SYSTEM_AS_OF at July 2024 -> 102
        res_pit2 = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c1, c2],
            snapshots=[s1, s2],
            filings=[f],
            mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
            as_of=datetime(2024, 7, 1, tzinfo=timezone.utc),
        )
        assert res_pit2.status == SECWinnerStatus.SELECTED
        assert res_pit2.selected_value == Decimal("102")

    def test_18_historical_snapshot_is_superseded_still_usable_in_pit(self):
        """Scenario 18: An old snapshot flagged is_superseded=True is still usable for historical SYSTEM_AS_OF."""
        s1 = _make_snapshot(retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc), is_superseded=True)
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c1 = _make_periodized_candidate(snapshot_id=s1.id, value=Decimal("100"))
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 1, 1))

        res = SECWinnerResolver.resolve_winner(
            economic_group_key=group_key,
            candidates=[c1],
            snapshots=[s1],
            filings=[f],
            mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
            as_of=datetime(2024, 3, 1, tzinfo=timezone.utc),
        )
        assert res.status == SECWinnerStatus.SELECTED
        assert res.selected_value == Decimal("100")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Candidate Eligibility Tests (Scenarios 19-28)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECCandidateEligibility:

    def test_19_and_20_primary_and_comparative_are_eligible(self):
        """Scenarios 19 & 20: PRIMARY_REPORT_PERIOD and COMPARATIVE_PRIOR_PERIOD are eligible."""
        s = _make_snapshot()
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c_prim = _make_periodized_candidate(snapshot_id=s.id, period_alignment_status=SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD)
        c_comp = _make_periodized_candidate(snapshot_id=s.id, period_alignment_status=SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD)
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))

        res_p = SECWinnerResolver.resolve_winner(group_key, [c_prim], [s], [f])
        assert res_p.status == SECWinnerStatus.SELECTED

        res_c = SECWinnerResolver.resolve_winner(group_key, [c_comp], [s], [f])
        assert res_c.status == SECWinnerStatus.SELECTED

    def test_21_to_28_ineligible_candidates_rejected(self):
        """Scenarios 21-28: Ineligible status, value None, Decimal 0, wrong snapshot."""
        s = _make_snapshot()
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))

        c_non_prim = _make_periodized_candidate(snapshot_id=s.id, period_alignment_status=SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT)
        c_unres = _make_periodized_candidate(snapshot_id=s.id, period_alignment_status=SECPeriodAlignmentStatus.UNRESOLVED_FILING)
        c_inv = _make_periodized_candidate(snapshot_id=s.id, period_alignment_status=SECPeriodAlignmentStatus.INVALID_CONTEXT)
        c_none_val = _make_periodized_candidate(snapshot_id=s.id, value=None)
        c_zero_val = _make_periodized_candidate(snapshot_id=s.id, value=Decimal("0.0"))

        assert SECWinnerResolver.resolve_winner(group_key, [c_non_prim], [s], [f]).status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE
        assert SECWinnerResolver.resolve_winner(group_key, [c_unres], [s], [f]).status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE
        assert SECWinnerResolver.resolve_winner(group_key, [c_inv], [s], [f]).status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE
        assert SECWinnerResolver.resolve_winner(group_key, [c_none_val], [s], [f]).status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE

        # Decimal("0.0") is valid
        assert SECWinnerResolver.resolve_winner(group_key, [c_zero_val], [s], [f]).status == SECWinnerStatus.SELECTED


# ─────────────────────────────────────────────────────────────────────────────
# 4. Filing Lineage Resolution Tests (Scenarios 29-35)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFilingLineageResolution:

    def test_29_to_35_lineage_resolution_and_validation(self):
        """Scenarios 29-35: Resolution via accession, filing_id, mismatch reject, form mismatch reject."""
        s = _make_snapshot()
        f_id = uuid4()
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, id=f_id, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # 29. filing_id resolution
        c_id = _make_periodized_candidate(snapshot_id=s.id, accession_number=None, filing_id=f_id)
        assert SECWinnerResolver.resolve_winner(group_key, [c_id], [s], [f]).status == SECWinnerStatus.SELECTED

        # 30. accession resolution
        c_acc = _make_periodized_candidate(snapshot_id=s.id, accession_number="0000320193-24-000106", filing_id=None)
        assert SECWinnerResolver.resolve_winner(group_key, [c_acc], [s], [f]).status == SECWinnerStatus.SELECTED

        # 33. Wrong CIK filing -> reject
        f_wrong_cik = SECFilingRecord(cik="789019", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        assert SECWinnerResolver.resolve_winner(group_key, [c_acc], [s], [f_wrong_cik]).status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE

        # 34. Wrong form -> reject
        f_8k = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="8-K", is_amendment=False, filing_date=date(2024, 9, 28))
        assert SECWinnerResolver.resolve_winner(group_key, [c_acc], [s], [f_8k]).status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE


# ─────────────────────────────────────────────────────────────────────────────
# 5. Disclosure Chronology Comparator Tests (Scenarios 36-42)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECDisclosureChronologyComparator:

    def test_36_to_42_disclosure_ordering_hierarchy(self):
        """Scenarios 36-42: Aware acceptance, local acceptance, filing_date fallback, unorderable."""
        # 36. Aware acceptance timestamps
        f_early = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_late = SECFilingRecord(cik="320193", accession_number="0002", form="10-K/A", is_amendment=True, acceptance_datetime=datetime(2024, 10, 1, 17, 30, 0, tzinfo=timezone.utc))
        assert compare_filing_disclosure_order(f_early, f_late) == FilingDisclosureComparison.B_LATER
        assert compare_filing_disclosure_order(f_late, f_early) == FilingDisclosureComparison.A_LATER

        # 37. Local naive acceptance timestamps
        f_loc1 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, acceptance_local_datetime=datetime(2024, 10, 1, 16, 0, 0), acceptance_timezone_semantics="SEC_EST_DOCUMENTED")
        f_loc2 = SECFilingRecord(cik="320193", accession_number="0002", form="10-K/A", is_amendment=True, acceptance_local_datetime=datetime(2024, 10, 1, 17, 0, 0), acceptance_timezone_semantics="SEC_EST_DOCUMENTED")
        assert compare_filing_disclosure_order(f_loc1, f_loc2) == FilingDisclosureComparison.B_LATER

        # 38. Different filing dates fallback
        f_d1 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 1, 15))
        f_d2 = SECFilingRecord(cik="320193", accession_number="0002", form="10-K/A", is_amendment=True, filing_date=date(2024, 2, 20))
        assert compare_filing_disclosure_order(f_d1, f_d2) == FilingDisclosureComparison.B_LATER

        # 40. Mixed representation + same filing date is UNORDERABLE
        f_aware = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc), filing_date=date(2024, 10, 1))
        f_naive = SECFilingRecord(cik="320193", accession_number="0002", form="10-K/A", is_amendment=True, acceptance_local_datetime=datetime(2024, 10, 1, 16, 0, 0), filing_date=date(2024, 10, 1))
        assert compare_filing_disclosure_order(f_aware, f_naive) == FilingDisclosureComparison.UNORDERABLE


# ─────────────────────────────────────────────────────────────────────────────
# 6. Same-Filing Semantic Reconciliation Tests (Scenarios 43-47)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSameFilingSemanticReconciliation:

    def test_43_to_47_same_filing_semantic_ranking(self):
        """Scenarios 43-47: EXACT vs COMPATIBLE, conflicting EXACT, corroborating duplicates."""
        s = _make_snapshot(retrieved_at=datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc))
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # 43. EXACT + COMPATIBLE same value -> EXACT selected
        c_exact = _make_periodized_candidate(snapshot_id=s.id, match_strength="EXACT", value=Decimal("100.0"))
        c_compat = _make_periodized_candidate(snapshot_id=s.id, match_strength="COMPATIBLE", value=Decimal("100.0"))
        res1 = SECWinnerResolver.resolve_winner(group_key, [c_exact, c_compat], [s], [f])
        assert res1.status == SECWinnerStatus.SELECTED
        assert res1.selected_candidate.id == c_exact.id
        assert c_compat.id in res1.corroborating_candidate_ids

        # 44. EXACT + COMPATIBLE different values -> EXACT selected
        c_compat_diff = _make_periodized_candidate(snapshot_id=s.id, match_strength="COMPATIBLE", value=Decimal("105.0"))
        res2 = SECWinnerResolver.resolve_winner(group_key, [c_exact, c_compat_diff], [s], [f])
        assert res2.status == SECWinnerStatus.SELECTED
        assert res2.selected_candidate.id == c_exact.id

        # 45. Two EXACT different values in same filing -> AMBIGUOUS_WITHIN_FILING
        c_exact_diff = _make_periodized_candidate(snapshot_id=s.id, match_strength="EXACT", value=Decimal("108.0"))
        res3 = SECWinnerResolver.resolve_winner(group_key, [c_exact, c_exact_diff], [s], [f])
        assert res3.status == SECWinnerStatus.AMBIGUOUS_WITHIN_FILING


# ─────────────────────────────────────────────────────────────────────────────
# 7. Amendment Precedence Tests (Scenarios 48-52)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECAmendmentPrecedence:

    def test_48_to_52_amendment_supersedes_original(self):
        """Scenarios 48-52: 10-K/A amendment overrides original 10-K when later."""
        s = _make_snapshot(retrieved_at=datetime(2024, 12, 1, 12, 0, 0, tzinfo=timezone.utc))
        f_orig = SECFilingRecord(cik="320193", accession_number="0000320193-24-000100", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_amend = SECFilingRecord(cik="320193", accession_number="0000320193-24-000105", form="10-K/A", is_amendment=True, filing_date=date(2024, 11, 1), acceptance_datetime=datetime(2024, 11, 1, 16, 0, 0, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c_orig = _make_periodized_candidate(snapshot_id=s.id, accession_number="0000320193-24-000100", form="10-K", is_amendment=False, value=Decimal("100"))
        c_amend = _make_periodized_candidate(snapshot_id=s.id, accession_number="0000320193-24-000105", form="10-K/A", is_amendment=True, value=Decimal("105"))

        res = SECWinnerResolver.resolve_winner(group_key, [c_orig, c_amend], [s], [f_orig, f_amend])
        assert res.status == SECWinnerStatus.SELECTED
        assert res.selected_value == Decimal("105")
        assert res.selected_accession_number == "0000320193-24-000105"
        assert c_orig.id in res.superseded_candidate_ids


# ─────────────────────────────────────────────────────────────────────────────
# 8. Comparative Restatement Precedence Tests (Scenarios 53-57)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECComparativeRestatementPrecedence:

    def test_53_to_57_later_comparative_restatement_wins_in_current_reported(self):
        """Scenarios 53-57: 2024 10-K comparative restatement of 2023 figures wins over original 2023 10-K."""
        s = _make_snapshot(retrieved_at=datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc))
        f_2023 = SECFilingRecord(cik="320193", accession_number="0000320193-23-000100", form="10-K", is_amendment=False, filing_date=date(2023, 10, 1), acceptance_datetime=datetime(2023, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_2024 = SECFilingRecord(cik="320193", accession_number="0000320193-24-000100", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2022-10-01", "2023-09-30")
        c_orig = _make_periodized_candidate(snapshot_id=s.id, accession_number="0000320193-23-000100", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), is_comparative=False, period_alignment_status=SECPeriodAlignmentStatus.PRIMARY_REPORT_PERIOD, value=Decimal("100"))
        c_restated = _make_periodized_candidate(snapshot_id=s.id, accession_number="0000320193-24-000100", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), is_comparative=True, period_alignment_status=SECPeriodAlignmentStatus.COMPARATIVE_PRIOR_PERIOD, value=Decimal("108"))

        res = SECWinnerResolver.resolve_winner(group_key, [c_orig, c_restated], [s], [f_2023, f_2024])
        assert res.status == SECWinnerStatus.SELECTED
        assert res.selected_value == Decimal("108")
        assert res.selected_accession_number == "0000320193-24-000100"
        assert c_orig.id in res.superseded_candidate_ids


# ─────────────────────────────────────────────────────────────────────────────
# 9. Cross-Filing Semantic Quality Conflict Policy (Scenarios 58-62)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECCrossFilingSemanticQualityPolicy:

    def test_58_to_62_semantic_quality_hierarchy(self):
        """Scenarios 58-62: Older EXACT vs later COMPATIBLE conflict policy."""
        s = _make_snapshot(retrieved_at=datetime(2024, 10, 1, 18, 0, 0, tzinfo=timezone.utc))
        f_old = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2023, 10, 1), acceptance_datetime=datetime(2023, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_new = SECFilingRecord(cik="320193", accession_number="0002", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2022-10-01", "2023-09-30")

        # 58. Older LEGACY 100 / Later EXACT 105 -> Later EXACT wins
        c_leg = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", match_strength="LEGACY_COMPATIBLE", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("100"))
        c_exact = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="EXACT", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("105"))
        assert SECWinnerResolver.resolve_winner(group_key, [c_leg, c_exact], [s], [f_old, f_new]).selected_value == Decimal("105")

        # 60. Older EXACT 100 / Later COMPATIBLE 100 -> Selected with MEDIUM confidence
        c_exact_100 = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", match_strength="EXACT", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("100"))
        c_compat_100 = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="COMPATIBLE", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("100"))
        res_same_val = SECWinnerResolver.resolve_winner(group_key, [c_exact_100, c_compat_100], [s], [f_old, f_new])
        assert res_same_val.status == SECWinnerStatus.SELECTED
        assert res_same_val.selection_confidence == "MEDIUM"

        # 61. Older EXACT 100 / Later COMPATIBLE 110 -> SEMANTIC_SCOPE_CONFLICT
        c_compat_110 = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="COMPATIBLE", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("110"))
        res_diff_val = SECWinnerResolver.resolve_winner(group_key, [c_exact_100, c_compat_110], [s], [f_old, f_new])
        assert res_diff_val.status == SECWinnerStatus.SEMANTIC_SCOPE_CONFLICT




# ─────────────────────────────────────────────────────────────────────────────
# 10. Mandatory Three-Snapshot Scenario (Section 77)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECThreeSnapshotIsolation:

    def test_mandatory_three_snapshot_isolation(self):
        """Section 77: S1 @ T1, S2 @ T2, S3 @ T3 isolation and candidate filtering."""
        t1 = datetime(2024, 1, 15, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 15, tzinfo=timezone.utc)
        t3 = datetime(2024, 11, 15, tzinfo=timezone.utc)

        s1 = _make_snapshot(retrieved_at=t1)
        s2 = _make_snapshot(retrieved_at=t2)
        s3 = _make_snapshot(retrieved_at=t3)

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        c1 = _make_periodized_candidate(snapshot_id=s1.id, value=Decimal("100"))
        c2 = _make_periodized_candidate(snapshot_id=s2.id, value=Decimal("200"))
        c3 = _make_periodized_candidate(snapshot_id=s3.id, value=Decimal("300"))

        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 1, 1))

        # CURRENT_REPORTED: only S3 candidate considered -> 300
        res_curr = SECWinnerResolver.resolve_winner(
            group_key, [c1, c2, c3], [s1, s2, s3], [f], mode=SECWinnerResolutionMode.CURRENT_REPORTED
        )
        assert res_curr.selected_value == Decimal("300")
        assert res_curr.evaluation_snapshot_id == s3.id

        # SYSTEM_AS_OF T2: only S2 candidate considered -> 200
        res_pit_t2 = SECWinnerResolver.resolve_winner(
            group_key, [c1, c2, c3], [s1, s2, s3], [f], mode=SECWinnerResolutionMode.SYSTEM_AS_OF, as_of=t2
        )
        assert res_pit_t2.selected_value == Decimal("200")
        assert res_pit_t2.evaluation_snapshot_id == s2.id


# ─────────────────────────────────────────────────────────────────────────────
# 11. Point-In-Time Lookahead Protection (Scenarios 69-73)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECPITLookaheadProtection:

    def test_69_to_73_lookahead_detection(self):
        """Scenarios 69-73: Impossible future filing date relative to as_of."""
        s = _make_snapshot(retrieved_at=datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc))
        f_future = SECFilingRecord(
            cik="320193",
            accession_number="0000320193-24-000106",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 9, 28),
            acceptance_datetime=datetime(2024, 9, 28, 16, 0, 0, tzinfo=timezone.utc),
        )
        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0000320193-24-000106")
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # as_of is March 2024, but filing is dated September 2024 -> INVALID_TEMPORAL_LINEAGE
        res = SECWinnerResolver.resolve_winner(
            group_key,
            [c],
            [s],
            [f_future],
            mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
            as_of=datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert res.status == SECWinnerStatus.INVALID_TEMPORAL_LINEAGE



# ─────────────────────────────────────────────────────────────────────────────
# 12. Result Auditability & Serialization (Scenarios 74-82)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECResultAuditability:

    def test_74_to_82_audit_fields_and_serialization(self):
        """Scenarios 74-82: Full audit trail validation."""
        s = _make_snapshot()
        f = SECFilingRecord(cik="320193", accession_number="0000320193-24-000106", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        c = _make_periodized_candidate(snapshot_id=s.id)
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        res = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f])
        assert res.status == SECWinnerStatus.SELECTED
        assert res.evaluation_snapshot_id == s.id
        assert res.evaluation_snapshot_hash == s.payload_hash
        assert res.selected_accession_number == "0000320193-24-000106"
        assert len(res.selection_basis) > 0

        d = res.to_dict()
        assert d["status"] == "selected"
        assert d["evaluation_snapshot_hash"] == s.payload_hash
        assert d["selected_value"] == "100000.00"


# ─────────────────────────────────────────────────────────────────────────────
# 13. Dual Filing Identifiers Hardening (Phase 8B.2B.5 Scenarios 1-8)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECDualFilingIdentifiersHardening:

    def test_01_to_03_valid_identifier_patterns(self):
        """Scenarios 1-3: Accession only, filing_id only, and both exact same filing are selected."""
        s = _make_snapshot()
        f_id = uuid4()
        f = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, id=f_id, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # 1. Accession only
        c_acc = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", filing_id=None)
        assert SECWinnerResolver.resolve_winner(group_key, [c_acc], [s], [f]).status == SECWinnerStatus.SELECTED

        # 2. Filing ID only
        c_id = _make_periodized_candidate(snapshot_id=s.id, accession_number=None, filing_id=f_id)
        assert SECWinnerResolver.resolve_winner(group_key, [c_id], [s], [f]).status == SECWinnerStatus.SELECTED

        # 3. Both exact matching
        c_both = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", filing_id=f_id)
        assert SECWinnerResolver.resolve_winner(group_key, [c_both], [s], [f]).status == SECWinnerStatus.SELECTED

    def test_04_to_06_mismatching_or_partially_unresolved_identifiers_fail_closed(self):
        """Scenarios 4-6: Accession -> Filing A and filing_id -> Filing B or unresolvable identifier rejects candidate."""
        s = _make_snapshot()
        f_a_id = uuid4()
        f_b_id = uuid4()
        f_a = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, id=f_a_id, filing_date=date(2024, 9, 28))
        f_b = SECFilingRecord(cik="320193", accession_number="0002", form="10-K", is_amendment=False, id=f_b_id, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # 4. Accession 0001 (Filing A) vs filing_id f_b_id (Filing B)
        c_conflict = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", filing_id=f_b_id)
        res_conf = SECWinnerResolver.resolve_winner(group_key, [c_conflict], [s], [f_a, f_b])
        assert res_conf.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE
        assert any("resolve to different filings" in r["reason"] for r in res_conf.rejected_candidates)

        # 5. Accession resolves to f_a, but filing_id is unknown UUID
        c_unres_id = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", filing_id=uuid4())
        res_unres_id = SECWinnerResolver.resolve_winner(group_key, [c_unres_id], [s], [f_a])
        assert res_unres_id.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE

        # 6. Filing ID resolves to f_a, but accession is unknown string
        c_unres_acc = _make_periodized_candidate(snapshot_id=s.id, accession_number="0099", filing_id=f_a_id)
        res_unres_acc = SECWinnerResolver.resolve_winner(group_key, [c_unres_acc], [s], [f_a])
        assert res_unres_acc.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE

    def test_07_and_08_duplicate_colliding_filings_fail_closed(self):
        """Scenarios 7 & 8: Duplicate conflicting filings for same accession or id fail closed."""
        s = _make_snapshot()
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # 7. Same accession "0001" with different forms
        f_dup1 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        f_dup2 = SECFilingRecord(cik="320193", accession_number="0001", form="8-K", is_amendment=False, filing_date=date(2024, 9, 28))

        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", filing_id=None)
        res = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f_dup1, f_dup2])
        assert res.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE
        assert any("Conflicting duplicate filings" in r["reason"] for r in res.rejected_candidates)


# ─────────────────────────────────────────────────────────────────────────────
# 14. Local Acceptance Semantics Hardening (Phase 8B.2B.5 Scenarios 9-14)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECLocalAcceptanceSemanticsHardening:

    def test_09_to_14_local_acceptance_semantics_rules(self):
        """Scenarios 9-14: Local acceptance requires verified SEC_EST_DOCUMENTED semantics."""
        # 9. Two local datetimes + both SEC_EST_DOCUMENTED -> Chronological compare allowed
        f1 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, acceptance_local_datetime=datetime(2024, 10, 1, 16, 0, 0), acceptance_timezone_semantics="SEC_EST_DOCUMENTED")
        f2 = SECFilingRecord(cik="320193", accession_number="0002", form="10-K/A", is_amendment=True, acceptance_local_datetime=datetime(2024, 10, 1, 17, 0, 0), acceptance_timezone_semantics="SEC_EST_DOCUMENTED")
        assert compare_filing_disclosure_order(f1, f2) == FilingDisclosureComparison.B_LATER

        # 10. Both local datetimes + semantics None -> NOT direct compare
        f1_none = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, acceptance_local_datetime=datetime(2024, 10, 1, 16, 0, 0), acceptance_timezone_semantics=None, filing_date=date(2024, 10, 1))
        f2_none = SECFilingRecord(cik="320193", accession_number="0002", form="10-K/A", is_amendment=True, acceptance_local_datetime=datetime(2024, 10, 1, 17, 0, 0), acceptance_timezone_semantics=None, filing_date=date(2024, 10, 1))
        assert compare_filing_disclosure_order(f1_none, f2_none) == FilingDisclosureComparison.UNORDERABLE

        # 11. One SEC_EST_DOCUMENTED / one None -> no direct local ordering
        f1_doc = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, acceptance_local_datetime=datetime(2024, 10, 1, 16, 0, 0), acceptance_timezone_semantics="SEC_EST_DOCUMENTED", filing_date=date(2024, 10, 1))
        assert compare_filing_disclosure_order(f1_doc, f2_none) == FilingDisclosureComparison.UNORDERABLE

        # 13. Uncomparable local semantics with different filing dates -> filing_date fallback
        f1_diff_date = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, acceptance_local_datetime=datetime(2024, 10, 1, 16, 0, 0), acceptance_timezone_semantics=None, filing_date=date(2024, 1, 15))
        f2_diff_date = SECFilingRecord(cik="320193", accession_number="0002", form="10-K/A", is_amendment=True, acceptance_local_datetime=datetime(2024, 10, 1, 17, 0, 0), acceptance_timezone_semantics=None, filing_date=date(2024, 2, 20))
        assert compare_filing_disclosure_order(f1_diff_date, f2_diff_date) == FilingDisclosureComparison.B_LATER


# ─────────────────────────────────────────────────────────────────────────────
# 15. Snapshot Temporal Lineage Hardening (Phase 8B.2B.5 Scenarios 15-19)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSnapshotTemporalLineageHardening:

    def test_15_aware_acceptance_after_snapshot_in_current_reported_is_invalid_temporal(self):
        """Scenario 15: Aware acceptance after snapshot retrieved_at in CURRENT_REPORTED -> INVALID_TEMPORAL_LINEAGE."""
        s = _make_snapshot(retrieved_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        f_future_acc = SECFilingRecord(
            cik="320193",
            accession_number="0001",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 1),
            acceptance_datetime=datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc),  # After snapshot!
        )
        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001")
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        res = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f_future_acc], mode=SECWinnerResolutionMode.CURRENT_REPORTED)
        assert res.status == SECWinnerStatus.INVALID_TEMPORAL_LINEAGE

    def test_16_aware_acceptance_after_snapshot_in_system_as_of_even_if_before_as_of(self):
        """Scenario 16: Acceptance > snapshot retrieved_at is invalid even if acceptance < as_of."""
        s = _make_snapshot(retrieved_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        f_future_acc = SECFilingRecord(
            cik="320193",
            accession_number="0001",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 1),
            acceptance_datetime=datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc),
        )
        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001")
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # as_of is Jan 5 (after Jan 2 acceptance), but snapshot is Jan 1 -> INVALID_TEMPORAL_LINEAGE
        res = SECWinnerResolver.resolve_winner(
            group_key, [c], [s], [f_future_acc],
            mode=SECWinnerResolutionMode.SYSTEM_AS_OF,
            as_of=datetime(2024, 1, 5, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert res.status == SECWinnerStatus.INVALID_TEMPORAL_LINEAGE

    def test_17_filing_date_after_snapshot_retrieved_date_is_invalid_temporal(self):
        """Scenario 17: Filing date after snapshot retrieved_at date -> INVALID_TEMPORAL_LINEAGE."""
        s = _make_snapshot(retrieved_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        f_future_date = SECFilingRecord(
            cik="320193",
            accession_number="0001",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 15),  # After Jan 1 snapshot
        )
        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001")
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        res = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f_future_date], mode=SECWinnerResolutionMode.CURRENT_REPORTED)
        assert res.status == SECWinnerStatus.INVALID_TEMPORAL_LINEAGE


# ─────────────────────────────────────────────────────────────────────────────
# 16. Logical Duplicate Snapshots Equivalence (Phase 8B.2B.5 Scenarios 20-24)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECLogicalDuplicateSnapshotsHardening:

    def test_20_to_23_logical_snapshot_equivalence_and_order_invariance(self):
        """Scenarios 20-23: Snapshots with identical retrieved_at and payload_hash are treated as single logical state."""
        t = datetime(2024, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        payload = {"cik": 320193, "facts": {"us-gaap": {"Revenues": {}}}}
        s1 = _make_snapshot(retrieved_at=t, raw_payload=payload)
        s2 = _make_snapshot(retrieved_at=t, raw_payload=payload)
        assert s1.id != s2.id
        assert s1.payload_hash == s2.payload_hash

        f = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # 20. Candidate on s1
        c1 = _make_periodized_candidate(snapshot_id=s1.id, accession_number="0001", value=Decimal("100"))
        res1 = SECWinnerResolver.resolve_winner(group_key, [c1], [s1, s2], [f])
        assert res1.status == SECWinnerStatus.SELECTED
        assert res1.selected_value == Decimal("100")

        # 21. Candidate on s2
        c2 = _make_periodized_candidate(snapshot_id=s2.id, accession_number="0001", value=Decimal("100"))
        res2 = SECWinnerResolver.resolve_winner(group_key, [c2], [s1, s2], [f])
        assert res2.status == SECWinnerStatus.SELECTED
        assert res2.selected_value == Decimal("100")

        # 22. Reverse snapshot input order -> identical result
        res_rev = SECWinnerResolver.resolve_winner(group_key, [c2], [s2, s1], [f])
        assert res_rev.status == SECWinnerStatus.SELECTED
        assert res_rev.selected_value == Decimal("100")
        assert res_rev.evaluation_snapshot_id == res2.evaluation_snapshot_id



# ─────────────────────────────────────────────────────────────────────────────
# 17. Cover-Date Defense-In-Depth (Phase 8B.2B.5 Scenarios 25-27)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECCoverDateDefenseInDepth:

    def test_25_to_27_cover_date_defense(self):
        """Scenarios 25-27: Only verified DEI EntityCommonStockSharesOutstanding is eligible for COVER_DATE_CONTEXT."""
        s = _make_snapshot()
        f = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "SHARES_OUTSTANDING", "shares", "cover_date_instant", None, "2024-10-18")

        # 25. Verified DEI
        c_dei = _make_periodized_candidate(
            snapshot_id=s.id,
            accession_number="0001",
            canonical_concept="SHARES_OUTSTANDING",
            unit="shares",
            economic_period_kind=SECEconomicPeriodKind.COVER_DATE_INSTANT,
            period_alignment_status=SECPeriodAlignmentStatus.COVER_DATE_CONTEXT,
            taxonomy="dei",
            source_concept="EntityCommonStockSharesOutstanding",
            start_date=None,
            end_date=date(2024, 10, 18),
        )
        res_dei = SECWinnerResolver.resolve_winner(group_key, [c_dei], [s], [f])
        assert res_dei.status == SECWinnerStatus.SELECTED

        # 26. us-gaap SHARES_OUTSTANDING artificially marked COVER_DATE_CONTEXT -> reject
        c_us_gaap = _make_periodized_candidate(
            snapshot_id=s.id,
            accession_number="0001",
            canonical_concept="SHARES_OUTSTANDING",
            unit="shares",
            economic_period_kind=SECEconomicPeriodKind.COVER_DATE_INSTANT,
            period_alignment_status=SECPeriodAlignmentStatus.COVER_DATE_CONTEXT,
            taxonomy="us-gaap",
            source_concept="CommonStockSharesOutstanding",
            start_date=None,
            end_date=date(2024, 10, 18),
        )
        res_us_gaap = SECWinnerResolver.resolve_winner(group_key, [c_us_gaap], [s], [f])
        assert res_us_gaap.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE

        # 27. Custom taxonomy artificially marked COVER_DATE_CONTEXT -> reject
        c_custom = _make_periodized_candidate(
            snapshot_id=s.id,
            accession_number="0001",
            canonical_concept="SHARES_OUTSTANDING",
            unit="shares",
            economic_period_kind=SECEconomicPeriodKind.COVER_DATE_INSTANT,
            period_alignment_status=SECPeriodAlignmentStatus.COVER_DATE_CONTEXT,
            taxonomy="custom",
            source_concept="CustomShares",
            start_date=None,
            end_date=date(2024, 10, 18),
        )
        res_custom = SECWinnerResolver.resolve_winner(group_key, [c_custom], [s], [f])
        assert res_custom.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE


# ─────────────────────────────────────────────────────────────────────────────
# 18. Order-Independent Disclosure Permutations (Phase 8B.2B.6 Scenarios 31-35)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECOrderIndependentPermutations:

    def test_31_intermediate_lower_quality_does_not_block_latest_exact(self):
        """Scenario 31: A (old EXACT 100) < B (mid COMPATIBLE 110) < C (latest EXACT 110).
        All 6 candidate permutations must select C = 110."""
        s = _make_snapshot(retrieved_at=datetime(2024, 12, 1, 18, 0, 0, tzinfo=timezone.utc))
        f_a = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2022, 10, 1), acceptance_datetime=datetime(2022, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_b = SECFilingRecord(cik="320193", accession_number="0002", form="10-K", is_amendment=False, filing_date=date(2023, 10, 1), acceptance_datetime=datetime(2023, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_c = SECFilingRecord(cik="320193", accession_number="0003", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2021-10-01", "2022-09-30")

        c_a = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", match_strength="EXACT", start_date=date(2021, 10, 1), end_date=date(2022, 9, 30), value=Decimal("100"))
        c_b = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="COMPATIBLE", start_date=date(2021, 10, 1), end_date=date(2022, 9, 30), value=Decimal("110"))
        c_c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0003", match_strength="EXACT", start_date=date(2021, 10, 1), end_date=date(2022, 9, 30), value=Decimal("110"))

        cands = [c_a, c_b, c_c]
        filings = [f_a, f_b, f_c]

        for p_cands in itertools.permutations(cands):
            for p_filings in itertools.permutations(filings):
                res = SECWinnerResolver.resolve_winner(group_key, list(p_cands), [s], list(p_filings))
                assert res.status == SECWinnerStatus.SELECTED
                assert res.selected_value == Decimal("110")
                assert res.selected_accession_number == "0003"
                assert res.selected_candidate.id == c_c.id
                assert c_a.id in res.superseded_candidate_ids
                assert c_b.id in res.corroborating_candidate_ids

    def test_32_three_exact_filings_all_permutations(self):
        """Scenario 32: A (100) < B (105) < C (110) all EXACT. All permutations select C = 110."""
        s = _make_snapshot(retrieved_at=datetime(2024, 12, 1, 18, 0, 0, tzinfo=timezone.utc))
        f_a = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2022, 10, 1), acceptance_datetime=datetime(2022, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_b = SECFilingRecord(cik="320193", accession_number="0002", form="10-K", is_amendment=False, filing_date=date(2023, 10, 1), acceptance_datetime=datetime(2023, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_c = SECFilingRecord(cik="320193", accession_number="0003", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2021-10-01", "2022-09-30")

        c_a = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", match_strength="EXACT", start_date=date(2021, 10, 1), end_date=date(2022, 9, 30), value=Decimal("100"))
        c_b = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="EXACT", start_date=date(2021, 10, 1), end_date=date(2022, 9, 30), value=Decimal("105"))
        c_c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0003", match_strength="EXACT", start_date=date(2021, 10, 1), end_date=date(2022, 9, 30), value=Decimal("110"))

        cands = [c_a, c_b, c_c]
        filings = [f_a, f_b, f_c]

        for p_cands in itertools.permutations(cands):
            for p_filings in itertools.permutations(filings):
                res = SECWinnerResolver.resolve_winner(group_key, list(p_cands), [s], list(p_filings))
                assert res.status == SECWinnerStatus.SELECTED
                assert res.selected_value == Decimal("110")
                assert res.selected_accession_number == "0003"
                assert c_a.id in res.superseded_candidate_ids
                assert c_b.id in res.superseded_candidate_ids

    def test_33_true_latest_lower_quality_conflict(self):
        """Scenario 33: A (old EXACT 100) < B (latest COMPATIBLE 110). Both input orders return SEMANTIC_SCOPE_CONFLICT."""
        s = _make_snapshot(retrieved_at=datetime(2024, 12, 1, 18, 0, 0, tzinfo=timezone.utc))
        f_a = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2023, 10, 1), acceptance_datetime=datetime(2023, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_b = SECFilingRecord(cik="320193", accession_number="0002", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2022-10-01", "2023-09-30")

        c_a = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", match_strength="EXACT", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("100"))
        c_b = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="COMPATIBLE", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("110"))

        for p_cands in itertools.permutations([c_a, c_b]):
            for p_filings in itertools.permutations([f_a, f_b]):
                res = SECWinnerResolver.resolve_winner(group_key, list(p_cands), [s], list(p_filings))
                assert res.status == SECWinnerStatus.SEMANTIC_SCOPE_CONFLICT

    def test_34_latest_lower_quality_same_value(self):
        """Scenario 34: A (old EXACT 100) < B (latest COMPATIBLE 100). Both input orders return SELECTED with MEDIUM confidence."""
        s = _make_snapshot(retrieved_at=datetime(2024, 12, 1, 18, 0, 0, tzinfo=timezone.utc))
        f_a = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2023, 10, 1), acceptance_datetime=datetime(2023, 10, 1, 16, 0, 0, tzinfo=timezone.utc))
        f_b = SECFilingRecord(cik="320193", accession_number="0002", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_datetime=datetime(2024, 10, 1, 16, 0, 0, tzinfo=timezone.utc))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2022-10-01", "2023-09-30")

        c_a = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", match_strength="EXACT", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("100"))
        c_b = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="COMPATIBLE", start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), value=Decimal("100"))

        for p_cands in itertools.permutations([c_a, c_b]):
            for p_filings in itertools.permutations([f_a, f_b]):
                res = SECWinnerResolver.resolve_winner(group_key, list(p_cands), [s], list(p_filings))
                assert res.status == SECWinnerStatus.SELECTED
                assert res.selected_value == Decimal("100")
                assert res.selected_accession_number == "0002"
                assert res.selection_confidence == "MEDIUM"

    def test_35_unorderable_frontier(self):
        """Scenario 35: A and B unorderable. Different values -> AMBIGUOUS_DISCLOSURE_ORDER. Same values -> SELECTED with MEDIUM."""
        s = _make_snapshot(retrieved_at=datetime(2024, 12, 1, 18, 0, 0, tzinfo=timezone.utc))
        f_a = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_local_datetime=datetime(2024, 10, 1, 16, 0, 0), acceptance_timezone_semantics=None)
        f_b = SECFilingRecord(cik="320193", accession_number="0002", form="10-K", is_amendment=False, filing_date=date(2024, 10, 1), acceptance_local_datetime=datetime(2024, 10, 1, 17, 0, 0), acceptance_timezone_semantics=None)

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        # Differing values -> AMBIGUOUS_DISCLOSURE_ORDER
        c_a_diff = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", value=Decimal("100"))
        c_b_diff = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", value=Decimal("110"))

        for p_cands in itertools.permutations([c_a_diff, c_b_diff]):
            res = SECWinnerResolver.resolve_winner(group_key, list(p_cands), [s], [f_a, f_b])
            assert res.status == SECWinnerStatus.AMBIGUOUS_DISCLOSURE_ORDER

        # Same values -> SELECTED with MEDIUM confidence
        c_a_same = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", value=Decimal("100"))
        c_b_same = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", value=Decimal("100"))

        for p_cands in itertools.permutations([c_a_same, c_b_same]):
            res = SECWinnerResolver.resolve_winner(group_key, list(p_cands), [s], [f_a, f_b])
            assert res.status == SECWinnerStatus.SELECTED
            assert res.selected_value == Decimal("100")
            assert res.selection_confidence == "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# 19. Logical Filing Deduplication & Dual ID Hardening (Phase 8B.2B.6 Scenarios 36-38)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECLogicalFilingDeduplication:

    def test_36_identical_logical_filing_duplicates_deduplicate_safely(self):
        """Scenario 36: Two filing records with same accession, different UUID, same logical metadata."""
        s = _make_snapshot()
        f1_id = uuid4()
        f2_id = uuid4()
        f1 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, id=f1_id, filing_date=date(2024, 9, 28))
        f2 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, id=f2_id, filing_date=date(2024, 9, 28))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", filing_id=None)

        res1 = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f1, f2])
        assert res1.status == SECWinnerStatus.SELECTED

        res2 = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f2, f1])
        assert res2.status == SECWinnerStatus.SELECTED

    def test_37_logical_duplicate_with_dual_id(self):
        """Scenario 37: Two equivalent filing records with same accession X, candidate has accession X and filing_id B."""
        s = _make_snapshot()
        f1_id = uuid4()
        f2_id = uuid4()
        f1 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, id=f1_id, filing_date=date(2024, 9, 28))
        f2 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, id=f2_id, filing_date=date(2024, 9, 28))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", filing_id=f2_id)

        res = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f1, f2])
        assert res.status == SECWinnerStatus.SELECTED

    def test_38_true_filing_collision_fails_closed(self):
        """Scenario 38: Conflicting filing records with same accession but different form/date metadata fail closed."""
        s = _make_snapshot()
        f1 = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        f2 = SECFilingRecord(cik="320193", accession_number="0001", form="10-Q", is_amendment=False, filing_date=date(2024, 9, 28))

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")
        c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001")

        res = SECWinnerResolver.resolve_winner(group_key, [c], [s], [f1, f2])
        assert res.status == SECWinnerStatus.NO_ELIGIBLE_CANDIDATE
        assert any("Conflicting duplicate filings" in r["reason"] for r in res.rejected_candidates)


# ─────────────────────────────────────────────────────────────────────────────
# 20. Same-Filing Deterministic Ties (Phase 8B.2B.6 Scenarios 39-41)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECSameFilingDeterministicTies:

    def test_39_same_filing_identical_rank_priority_value_tie_break(self):
        """Scenario 39: Two same-filing candidates with identical quality rank, priority, and value.
        Reverse input order yields identical selected representation."""
        s = _make_snapshot()
        f = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        c1 = _make_periodized_candidate(
            snapshot_id=s.id, accession_number="0001", match_strength="EXACT", variant_priority=10,
            source_concept="ConceptAlpha", value=Decimal("100")
        )
        c2 = _make_periodized_candidate(
            snapshot_id=s.id, accession_number="0001", match_strength="EXACT", variant_priority=10,
            source_concept="ConceptBeta", value=Decimal("100")
        )

        res1 = SECWinnerResolver.resolve_winner(group_key, [c1, c2], [s], [f])
        res2 = SECWinnerResolver.resolve_winner(group_key, [c2, c1], [s], [f])

        assert res1.status == SECWinnerStatus.SELECTED
        assert res2.status == SECWinnerStatus.SELECTED
        assert res1.selected_source_concept == res2.selected_source_concept
        assert res1.selected_candidate.id == res2.selected_candidate.id
        assert res1.selected_value == res2.selected_value == Decimal("100")

    def test_conflicting_exact_values_in_same_filing_still_ambiguous(self):
        """Preserve: differing values for EXACT candidates in same filing still returns AMBIGUOUS_WITHIN_FILING."""
        s = _make_snapshot()
        f = SECFilingRecord(cik="320193", accession_number="0001", form="10-K", is_amendment=False, filing_date=date(2024, 9, 28))
        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-10-01", "2024-09-28")

        c1 = _make_periodized_candidate(
            snapshot_id=s.id, accession_number="0001", match_strength="EXACT", variant_priority=10,
            source_concept="ConceptAlpha", value=Decimal("100")
        )
        c2 = _make_periodized_candidate(
            snapshot_id=s.id, accession_number="0001", match_strength="EXACT", variant_priority=10,
            source_concept="ConceptBeta", value=Decimal("105")
        )

        res1 = SECWinnerResolver.resolve_winner(group_key, [c1, c2], [s], [f])
        assert res1.status == SECWinnerStatus.AMBIGUOUS_WITHIN_FILING
        res2 = SECWinnerResolver.resolve_winner(group_key, [c2, c1], [s], [f])
        assert res2.status == SECWinnerStatus.AMBIGUOUS_WITHIN_FILING


# ─────────────────────────────────────────────────────────────────────────────
# 21. Chronology Graph Consistency & Cycle Defense (Phase 8B.2B.7 Scenarios 10-16)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECChronologyGraphAndCycleDefense:

    def test_10_and_11_directed_cycle_fails_closed_across_all_permutations(self):
        """Scenarios 10 & 11: Mixed metadata produces cyclic chronology A -> C -> B -> A.
        Must return CHRONOLOGY_CONFLICT with winner=None for all candidate and filing permutations."""
        # Evaluation snapshot retrieved after all filing/acceptance events
        s = _make_snapshot(retrieved_at=datetime(2024, 1, 10, 18, 0, 0, tzinfo=timezone.utc))

        f_a = SECFilingRecord(
            cik="320193",
            accession_number="0001",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 1),
            acceptance_datetime=datetime(2024, 1, 3, 16, 0, 0, tzinfo=timezone.utc),
        )
        f_b = SECFilingRecord(
            cik="320193",
            accession_number="0002",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 3),
            acceptance_datetime=datetime(2024, 1, 2, 16, 0, 0, tzinfo=timezone.utc),
        )
        f_c = SECFilingRecord(
            cik="320193",
            accession_number="0003",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 2),
            acceptance_datetime=None,
            acceptance_local_datetime=None,
        )

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-01-01", "2023-12-31")

        c_a = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", value=Decimal("100"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
        c_b = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", value=Decimal("105"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
        c_c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0003", value=Decimal("110"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))

        # Test all 6 candidate permutations and all 6 filing permutations
        for p_cands in itertools.permutations([c_a, c_b, c_c]):
            for p_filings in itertools.permutations([f_a, f_b, f_c]):
                res = SECWinnerResolver.resolve_winner(group_key, list(p_cands), [s], list(p_filings))
                assert res.status == SECWinnerStatus.CHRONOLOGY_CONFLICT
                assert res.selected_candidate is None
                assert res.selected_value is None
                assert "cycle across filings" in res.selection_basis

    def test_16_mixed_basis_acyclic_resolves_successfully(self):
        """Scenario 16: Mixed metadata basis (aware vs filing_date) that is acyclic resolves correctly."""
        s = _make_snapshot(retrieved_at=datetime(2024, 1, 10, 18, 0, 0, tzinfo=timezone.utc))

        f_a = SECFilingRecord(
            cik="320193",
            accession_number="0001",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 1),
            acceptance_datetime=datetime(2024, 1, 1, 16, 0, 0, tzinfo=timezone.utc),
        )
        f_b = SECFilingRecord(
            cik="320193",
            accession_number="0002",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 2),
            acceptance_datetime=datetime(2024, 1, 2, 16, 0, 0, tzinfo=timezone.utc),
        )
        f_c = SECFilingRecord(
            cik="320193",
            accession_number="0003",
            form="10-K",
            is_amendment=False,
            filing_date=date(2024, 1, 3),
            acceptance_datetime=None,
        )

        group_key = ("0000320193", "REVENUE", "USD", "annual_duration", "2023-01-01", "2023-12-31")

        c_a = _make_periodized_candidate(snapshot_id=s.id, accession_number="0001", match_strength="EXACT", value=Decimal("100"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
        c_b = _make_periodized_candidate(snapshot_id=s.id, accession_number="0002", match_strength="EXACT", value=Decimal("105"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
        c_c = _make_periodized_candidate(snapshot_id=s.id, accession_number="0003", match_strength="EXACT", value=Decimal("110"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))

        # A (Jan 1) < B (Jan 2) < C (Jan 3) is acyclic
        res = SECWinnerResolver.resolve_winner(group_key, [c_a, c_b, c_c], [s], [f_a, f_b, f_c])
        assert res.status == SECWinnerStatus.SELECTED
        assert res.selected_value == Decimal("110")
        assert res.selected_accession_number == "0003"




