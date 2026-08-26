"""
backend/tests/test_sec_concept_resolver.py
============================================
Comprehensive Unit Test Suite for SEC EDGAR Phase 8B.1:
Canonical Financial Concept Families & Raw-Fact Candidate Resolver.

Coverage:
    - Canonical Concept Registry & Authoritative Verification (Scenarios 1-9)
    - Core Concept Mapping & Taxonomies (Scenarios 10-22)
    - Semantic Guards, Unit & Period Type Validation (Scenarios 23-33)
    - Decimal Precision & Provenance Preservation (Scenarios 34-43)
    - Form Role Classification (Scenarios 44-51)
    - No-Winner & Multi-Candidate Invariants (Scenarios 52-57)
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from backend.engine.private.sec.concept_resolver import (
    SECConceptResolver,
    classify_form_role,
    validate_unit_compatibility,
)
from backend.engine.private.sec.concepts import (
    CanonicalSECConceptDefinition,
    ConceptVariant,
    FormRole,
    MatchStrength,
    PeriodType,
    SECConceptMatchStatus,
    StatementFamily,
    UnitClass,
    VerificationStatus,
    get_initial_canonical_concept_definitions,
)
from backend.engine.private.sec.models import (
    SECCanonicalFactCandidate,
    SECFilingRecord,
    SECRawFactRecord,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Registry & Authoritative Verification Tests (Scenarios 1-9)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECConceptRegistry:

    def test_01_canonical_concept_names_unique(self):
        """Scenario 1: All canonical concept names in the registry must be unique."""
        definitions = get_initial_canonical_concept_definitions()
        names = [d.canonical_concept for d in definitions]
        assert len(names) == len(set(names)), "Duplicate canonical concept names detected."

    def test_02_and_03_no_active_duplicate_variant_across_canonical_concepts(self):
        """Scenario 2 & 3: No single active (taxonomy, tag) pair maps to more than one canonical concept."""
        definitions = get_initial_canonical_concept_definitions()
        seen_tags = {}
        for defn in definitions:
            if not defn.active:
                continue
            for variant in defn.variants:
                if not variant.active:
                    continue
                key = (variant.taxonomy.strip().lower(), variant.tag.strip().lower())
                assert key not in seen_tags, (
                    f"Ambiguous variant detected! Tag {key} is mapped to both "
                    f"'{seen_tags.get(key)}' and '{defn.canonical_concept}'."
                )
                seen_tags[key] = defn.canonical_concept

    def test_04_and_07_unverified_variant_cannot_be_active(self):
        """Scenario 4 & 7: Variant cannot be active without VERIFIED_OFFICIAL status and verification source citation."""
        with pytest.raises(ValueError, match="cannot be ACTIVE without VERIFIED_OFFICIAL status"):
            ConceptVariant(
                taxonomy="us-gaap",
                tag="FakeUnverifiedTag",
                match_strength=MatchStrength.COMPATIBLE,
                priority=1,
                semantic_scope="Test",
                verification_source="None",
                verification_status=VerificationStatus.UNVERIFIED,
                active=True,
            )

        with pytest.raises(ValueError, match="requires an authoritative verification_source citation"):
            ConceptVariant(
                taxonomy="us-gaap",
                tag="FakeNoSourceTag",
                match_strength=MatchStrength.COMPATIBLE,
                priority=1,
                semantic_scope="Test",
                verification_source="",
                verification_status=VerificationStatus.VERIFIED_OFFICIAL,
                active=True,
            )

    def test_05_and_06_expected_period_and_unit_class_explicit(self):
        """Scenario 5 & 6: Every canonical concept definition explicitly declares expected period and unit class."""
        definitions = get_initial_canonical_concept_definitions()
        for defn in definitions:
            assert isinstance(defn.expected_period_type, PeriodType)
            assert isinstance(defn.expected_unit_class, UnitClass)
            assert isinstance(defn.statement_family, StatementFamily)

    def test_08_and_09_exact_vs_legacy_distinction_preserved(self):
        """Scenario 8 & 9: Exact modern vs legacy variants are distinguished with correct match strength and priorities."""
        definitions = get_initial_canonical_concept_definitions()
        rev_defn = next(d for d in definitions if d.canonical_concept == "REVENUE")

        modern_v = next(v for v in rev_defn.variants if v.tag == "RevenueFromContractWithCustomerExcludingAssessedTax")
        assert modern_v.match_strength == MatchStrength.EXACT
        assert modern_v.legacy_status is False
        assert modern_v.priority == 1

        legacy_v = next(v for v in rev_defn.variants if v.tag == "SalesRevenueNet")
        assert legacy_v.match_strength == MatchStrength.LEGACY_COMPATIBLE
        assert legacy_v.legacy_status is True
        assert legacy_v.priority == 3


# ─────────────────────────────────────────────────────────────────────────────
# 2. Core Mapping Tests (Scenarios 10-22)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECConceptResolverCoreMapping:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_10_and_11_assets_and_liabilities_instant(self, resolver: SECConceptResolver):
        """Scenario 10 & 11: Assets & Liabilities instant balance sheet items resolve to TOTAL_ASSETS & TOTAL_LIABILITIES."""
        fact_assets = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="Assets",
            unit="USD",
            period_type=PeriodType.INSTANT,
            value=Decimal("364980000000"),
            end_date=date(2024, 9, 28),
        )
        cand_a, status_a, _ = resolver.resolve_raw_fact(fact_assets)
        assert status_a == SECConceptMatchStatus.MATCHED
        assert cand_a.canonical_concept == "TOTAL_ASSETS"
        assert cand_a.match_strength == "exact"
        assert cand_a.value == Decimal("364980000000")

        fact_liab = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="Liabilities",
            unit="USD",
            period_type=PeriodType.INSTANT,
            value=Decimal("200000000000"),
            end_date=date(2024, 9, 28),
        )
        cand_l, status_l, _ = resolver.resolve_raw_fact(fact_liab)
        assert status_l == SECConceptMatchStatus.MATCHED
        assert cand_l.canonical_concept == "TOTAL_LIABILITIES"

    def test_12_to_14_operating_income_net_income_and_revenue(self, resolver: SECConceptResolver):
        """Scenario 12-14: Income statement duration concepts resolve correctly."""
        fact_op = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="OperatingIncomeLoss",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("123000000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_op, status_op, _ = resolver.resolve_raw_fact(fact_op)
        assert status_op == SECConceptMatchStatus.MATCHED
        assert cand_op.canonical_concept == "OPERATING_INCOME"

        fact_net = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="NetIncomeLoss",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("96995000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_net, status_net, _ = resolver.resolve_raw_fact(fact_net)
        assert status_net == SECConceptMatchStatus.MATCHED
        assert cand_net.canonical_concept == "NET_INCOME"

        fact_rev = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("391035000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_rev, status_rev, _ = resolver.resolve_raw_fact(fact_rev)
        assert status_rev == SECConceptMatchStatus.MATCHED
        assert cand_rev.canonical_concept == "REVENUE"
        assert cand_rev.match_strength == "exact"

    def test_15_to_19_current_items_cash_cfo_and_capex(self, resolver: SECConceptResolver):
        """Scenario 15-19: Current Assets/Liab, Cash, CFO, and CapEx resolve to canonical concepts."""
        # Current Assets
        f_ca = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="AssetsCurrent", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_ca)[0].canonical_concept == "CURRENT_ASSETS"

        # Current Liabilities
        f_cl = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="LiabilitiesCurrent", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("50"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_cl)[0].canonical_concept == "CURRENT_LIABILITIES"

        # Cash & Cash Equivalents
        f_cash = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="CashAndCashEquivalentsAtCarryingValue", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("30000000000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_cash)[0].canonical_concept == "CASH_AND_CASH_EQUIVALENTS"

        # Operating Cash Flow (CFO)
        f_cfo = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="NetCashProvidedByUsedInOperatingActivities", unit="USD", period_type=PeriodType.DURATION, value=Decimal("118000000000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_cfo)[0].canonical_concept == "OPERATING_CASH_FLOW"

        # CapEx PP&E
        f_capex = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="PaymentsToAcquirePropertyPlantAndEquipment", unit="USD", period_type=PeriodType.DURATION, value=Decimal("9500000000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        cand_capex, _, _ = resolver.resolve_raw_fact(f_capex)
        assert cand_capex.canonical_concept == "CAPEX_PP&E"
        assert cand_capex.value == Decimal("9500000000")  # Raw positive outflow preserved without sign flip

    def test_20_to_22_diluted_eps_and_shares(self, resolver: SECConceptResolver):
        """Scenario 20-22: Diluted EPS, Diluted Shares, and DEI Shares Outstanding resolve correctly."""
        # Diluted EPS
        f_eps = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="EarningsPerShareDiluted", unit="USD/shares", period_type=PeriodType.DURATION, value=Decimal("6.08"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        cand_eps, _, _ = resolver.resolve_raw_fact(f_eps)
        assert cand_eps.canonical_concept == "DILUTED_EPS"
        assert cand_eps.value == Decimal("6.08")

        # Diluted Weighted Shares
        f_wshares = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="WeightedAverageNumberOfDilutedSharesOutstanding", unit="shares", period_type=PeriodType.DURATION, value=Decimal("15700000000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_wshares)[0].canonical_concept == "DILUTED_WEIGHTED_AVERAGE_SHARES"

        # DEI Shares Outstanding
        f_dei = SECRawFactRecord(cik="320193", taxonomy="dei", concept="EntityCommonStockSharesOutstanding", unit="shares", period_type=PeriodType.INSTANT, value=Decimal("15115820000"), end_date=date(2024, 10, 18))
        assert resolver.resolve_raw_fact(f_dei)[0].canonical_concept == "SHARES_OUTSTANDING"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Semantic Guards, Unit & Period Validation (Scenarios 23-33)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECConceptResolverSemanticGuards:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_23_and_24_period_type_mismatch_rejected(self, resolver: SECConceptResolver):
        """Scenario 23 & 24: Assets with DURATION and Revenue with INSTANT are rejected with INVALID_PERIOD_TYPE."""
        # Assets with DURATION -> Invalid
        f_bad_assets = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="Assets", unit="USD", period_type=PeriodType.DURATION, value=Decimal("100"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        cand, status, diags = resolver.resolve_raw_fact(f_bad_assets)
        assert status == SECConceptMatchStatus.INVALID_PERIOD_TYPE
        assert cand is None

        # Revenue with INSTANT -> Invalid
        f_bad_rev = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="Revenues", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        cand_r, status_r, _ = resolver.resolve_raw_fact(f_bad_rev)
        assert status_r == SECConceptMatchStatus.INVALID_PERIOD_TYPE
        assert cand_r is None

    def test_25_to_27_unit_mismatch_rejected(self, resolver: SECConceptResolver):
        """Scenario 25-27: Shares with USD unit or Monetary items with shares unit are rejected with INVALID_UNIT."""
        # Shares outstanding with USD unit
        f_bad_shares = SECRawFactRecord(cik="320193", taxonomy="dei", concept="EntityCommonStockSharesOutstanding", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 10, 18))
        cand, status, diags = resolver.resolve_raw_fact(f_bad_shares)
        assert status == SECConceptMatchStatus.INVALID_UNIT
        assert cand is None

        # Revenue with shares unit
        f_bad_rev_u = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="Revenues", unit="shares", period_type=PeriodType.DURATION, value=Decimal("100"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_bad_rev_u)[1] == SECConceptMatchStatus.INVALID_UNIT

        # EPS with plain USD (missing per-share denominator)
        f_bad_eps = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="EarningsPerShareDiluted", unit="USD", period_type=PeriodType.DURATION, value=Decimal("6.08"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_bad_eps)[1] == SECConceptMatchStatus.INVALID_UNIT

    def test_28_to_30_specialized_and_narrow_concepts_not_overmapped(self, resolver: SECConceptResolver):
        """Scenario 28-30: Restricted cash, equity with NCI, and interest income are NOT mapped to plain generic concepts."""
        # Restricted cash combined tag
        f_rcash = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("50000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_rcash)[1] == SECConceptMatchStatus.NO_MATCH

        # Interest Income
        f_interest = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="InterestIncome", unit="USD", period_type=PeriodType.DURATION, value=Decimal("50000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_interest)[1] == SECConceptMatchStatus.NO_MATCH

        # StockholdersEquity vs StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest
        f_nci = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("70000"), end_date=date(2024, 9, 28))
        cand_nci, _, _ = resolver.resolve_raw_fact(f_nci)
        assert cand_nci.canonical_concept == "EQUITY_INCLUDING_NCI"
        assert cand_nci.canonical_concept != "EQUITY_ATTRIBUTABLE_TO_PARENT"

    def test_31_to_33_unknown_and_ambiguous_tags(self, resolver: SECConceptResolver):
        """Scenario 31-33: Unknown tags return NO_MATCH; ambiguous definitions fail-closed with AMBIGUOUS."""
        f_unknown = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="TotallyUnknownCustomConcept123", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_unknown)[1] == SECConceptMatchStatus.NO_MATCH

        # Test ambiguous injection
        ambig_def1 = CanonicalSECConceptDefinition(
            canonical_concept="TEST_CONCEPT_A",
            description="Test A",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[ConceptVariant(taxonomy="us-gaap", tag="AmbiguousTag", match_strength=MatchStrength.EXACT, priority=1, semantic_scope="", verification_source="Test")],
        )
        ambig_def2 = CanonicalSECConceptDefinition(
            canonical_concept="TEST_CONCEPT_B",
            description="Test B",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[ConceptVariant(taxonomy="us-gaap", tag="AmbiguousTag", match_strength=MatchStrength.EXACT, priority=1, semantic_scope="", verification_source="Test")],
        )
        ambig_resolver = SECConceptResolver(definitions=[ambig_def1, ambig_def2])
        f_ambig = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="AmbiguousTag", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        assert ambig_resolver.resolve_raw_fact(f_ambig)[1] == SECConceptMatchStatus.AMBIGUOUS


# ─────────────────────────────────────────────────────────────────────────────
# 4. Decimal Precision & Provenance Preservation (Scenarios 34-43)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECConceptResolverProvenanceAndPrecision:

    def test_34_to_43_provenance_and_exact_decimal_preservation(self):
        """Scenario 34-43: Value (Decimal), unit, taxonomy, accession, snapshot_id, and lineage are preserved verbatim."""
        snap_id = uuid4()
        fact = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            taxonomy="us-gaap",
            concept="Revenues",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("391035000000.50"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
            form="10-K",
            fiscal_year=2024,
            fiscal_period="FY",
            filed_date=date(2024, 11, 1),
            frame="CY2024",
            snapshot_id=snap_id,
        )

        filing = SECFilingRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            form="10-K",
            is_amendment=False,
        )

        resolver = SECConceptResolver()
        cand, status, _ = resolver.resolve_raw_fact(fact, filings_by_accession={"0000320193-24-000106": filing})

        assert status == SECConceptMatchStatus.MATCHED
        assert cand.value == Decimal("391035000000.50")
        assert cand.unit == "USD"
        assert cand.taxonomy == "us-gaap"
        assert cand.source_concept == "Revenues"
        assert cand.accession_number == "0000320193-24-000106"
        assert cand.snapshot_id == snap_id
        assert cand.filing_id == filing.id
        assert cand.lineage_status == "RESOLVED"
        assert cand.form_role == "primary_annual"
        assert cand.is_amendment is False


# ─────────────────────────────────────────────────────────────────────────────
# 5. Form Role Classification (Scenarios 44-51)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFormRoleClassification:

    def test_44_to_51_form_role_classification(self):
        """Scenario 44-51: Form strings map deterministically to FormRole without declaring filing winners."""
        assert classify_form_role("10-K") == (FormRole.PRIMARY_ANNUAL, False)
        assert classify_form_role("10-K/A") == (FormRole.AMENDMENT_ANNUAL, True)
        assert classify_form_role("10-Q") == (FormRole.PRIMARY_QUARTERLY, False)
        assert classify_form_role("10-Q/A") == (FormRole.AMENDMENT_QUARTERLY, True)
        assert classify_form_role("20-F") == (FormRole.FPI_ANNUAL, False)
        assert classify_form_role("20-F/A") == (FormRole.FPI_AMENDMENT_ANNUAL, True)
        assert classify_form_role("40-F") == (FormRole.FPI_ANNUAL, False)
        assert classify_form_role("6-K") == (FormRole.FPI_INTERIM_OR_EVENT, False)
        assert classify_form_role("8-K") == (FormRole.EVENT_FILING, False)


# ─────────────────────────────────────────────────────────────────────────────
# 6. No-Winner & Multi-Candidate Invariants (Scenarios 52-57)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECNoWinnerInvariants:

    def test_52_to_57_multi_accession_amendments_and_comparatives_all_preserved(self):
        """Scenario 52-57: Same concept/period across multiple accessions (amendments/comparatives) are all preserved."""
        # 1. 2023 10-K fact
        fact_orig = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-23-000106",
            taxonomy="us-gaap",
            concept="Revenues",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("383285000000"),
            start_date=date(2022, 10, 1),
            end_date=date(2023, 9, 30),
            form="10-K",
            filed_date=date(2023, 11, 3),
        )

        # 2. 2024 10-K comparative column fact for 2023
        fact_comp = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            taxonomy="us-gaap",
            concept="Revenues",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("383285000000"),
            start_date=date(2022, 10, 1),
            end_date=date(2023, 9, 30),
            form="10-K",
            filed_date=date(2024, 11, 1),
        )

        # 3. 10-K/A amendment fact
        fact_amend = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-23-000999",
            taxonomy="us-gaap",
            concept="Revenues",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("383200000000"),
            start_date=date(2022, 10, 1),
            end_date=date(2023, 9, 30),
            form="10-K/A",
            filed_date=date(2023, 12, 1),
        )

        resolver = SECConceptResolver()
        candidates = resolver.resolve_facts([fact_orig, fact_comp, fact_amend])

        # All 3 candidates MUST be preserved without winner selection
        assert len(candidates) == 3
        accessions = {c.accession_number for c in candidates}
        assert accessions == {"0000320193-23-000106", "0000320193-24-000106", "0000320193-23-000999"}
        assert any(c.is_amendment for c in candidates)
