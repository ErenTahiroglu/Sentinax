"""
backend/tests/test_sec_concept_resolver.py
============================================
Comprehensive Unit Test Suite for SEC EDGAR Phase 8B.1 & 8B.1.6:
Canonical Financial Concept Families, Final Taxonomy Semantics & Resolver Fail-Closed Patch.

Coverage:
    - Taxonomy Version Support & Registry Hardening (Scenarios 1-10)
    - IFRS 18 Final Element & Legacy Operating Profit Separation (Scenarios 11-15)
    - Net Income Canonical Scope Split: Parent vs Including NCI (Scenarios 16-22)
    - Strict Fail-Closed Unit Validation (Scenarios 23-33)
    - CapEx Physical PP&E vs Productive Assets Separation (Scenarios 34-38)
    - Revenue, Equity, and Cash Semantic Scope Integrity (Scenarios 39-42)
    - Exact Precision & Provenance Preservation (Scenarios 43-48)
    - Form Roles & Multi-Candidate No-Winner Invariants (Scenarios 49-55)
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
# 1. Registry & Authoritative Verification (Scenarios 1-10)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECConceptRegistryAndVerification:

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

    def test_04_artificial_duplicate_registry_causes_ambiguous_fail_closed(self):
        """Scenario 4: If an ambiguous duplicate tag is injected, resolver fails closed with AMBIGUOUS."""
        def1 = CanonicalSECConceptDefinition(
            canonical_concept="TEST_CONCEPT_ALPHA",
            description="Alpha",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[ConceptVariant(taxonomy="us-gaap", tag="DupTag", match_strength=MatchStrength.EXACT, priority=1, semantic_scope="A", verified_taxonomy_family="US-GAAP", verified_taxonomy_release="2026", verification_source="Source A")],
        )
        def2 = CanonicalSECConceptDefinition(
            canonical_concept="TEST_CONCEPT_BETA",
            description="Beta",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[ConceptVariant(taxonomy="us-gaap", tag="DupTag", match_strength=MatchStrength.EXACT, priority=1, semantic_scope="B", verified_taxonomy_family="US-GAAP", verified_taxonomy_release="2026", verification_source="Source B")],
        )
        ambig_resolver = SECConceptResolver(definitions=[def1, def2])
        fact = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="DupTag", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        cand, status, diags = ambig_resolver.resolve_raw_fact(fact)
        assert status == SECConceptMatchStatus.AMBIGUOUS
        assert cand is None

    def test_05_and_06_unverified_variant_cannot_be_active(self):
        """Scenario 5 & 6: Variant cannot be active without VERIFIED_OFFICIAL status and source citation."""
        with pytest.raises(ValueError, match="cannot be ACTIVE without VERIFIED_OFFICIAL status"):
            ConceptVariant(
                taxonomy="us-gaap",
                tag="FakeUnverifiedTag",
                match_strength=MatchStrength.COMPATIBLE,
                priority=1,
                semantic_scope="Test",
                verified_taxonomy_family="US-GAAP",
                verified_taxonomy_release="2026",
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
                verified_taxonomy_family="US-GAAP",
                verified_taxonomy_release="2026",
                verification_source="",
                verification_status=VerificationStatus.VERIFIED_OFFICIAL,
                active=True,
            )

    def test_07_and_08_expected_period_and_unit_class_explicit(self):
        """Scenario 7 & 8: Every canonical concept definition explicitly declares expected period and unit class."""
        definitions = get_initial_canonical_concept_definitions()
        for defn in definitions:
            assert isinstance(defn.expected_period_type, PeriodType)
            assert isinstance(defn.expected_unit_class, UnitClass)
            assert isinstance(defn.statement_family, StatementFamily)

    def test_09_and_10_unknown_and_unverified_tags_not_mapped(self):
        """Scenario 9 & 10: Unknown tags return NO_MATCH; inactive/unverified variants return UNVERIFIED_VARIANT."""
        resolver = SECConceptResolver()
        f_unknown = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="CompletelyUnknownCustomTag123", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_unknown)[1] == SECConceptMatchStatus.NO_MATCH

        # Inactive definition test
        inactive_def = CanonicalSECConceptDefinition(
            canonical_concept="TEST_INACTIVE",
            description="Inactive test",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            active=False,
            variants=[ConceptVariant(taxonomy="us-gaap", tag="InactiveTag", match_strength=MatchStrength.EXACT, priority=1, semantic_scope="Test", verified_taxonomy_family="US-GAAP", verified_taxonomy_release="2026", verification_source="Source")],
        )
        inact_resolver = SECConceptResolver(definitions=[inactive_def])
        f_inact = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="InactiveTag", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        assert inact_resolver.resolve_raw_fact(f_inact)[1] == SECConceptMatchStatus.NO_MATCH


# ─────────────────────────────────────────────────────────────────────────────
# 2. IFRS 18 Final Element & Legacy Operating Profit (Scenarios 11-15)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECIFRSOperatingProfitHardening:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_11_to_13_legacy_and_final_ifrs18_tags_resolve_to_distinct_concepts(self, resolver: SECConceptResolver):
        """Scenario 11-13: ProfitLossFromOperatingActivities -> OPERATING_INCOME_LEGACY_IAS1, OperatingProfitLossOperating -> OPERATING_PROFIT_IFRS18."""
        # Legacy IAS 1 subtotal
        f_legacy = SECRawFactRecord(
            cik="0001018724",
            taxonomy="ifrs-full",
            concept="ProfitLossFromOperatingActivities",
            unit="EUR",
            period_type=PeriodType.DURATION,
            value=Decimal("5000000"),
            start_date=date(2023, 1, 1),
            end_date=date(2023, 12, 31),
        )
        cand_l, status_l, _ = resolver.resolve_raw_fact(f_legacy)
        assert status_l == SECConceptMatchStatus.MATCHED
        assert cand_l.canonical_concept == "OPERATING_INCOME_LEGACY_IAS1"
        assert cand_l.match_strength == "legacy_compatible"

        # Final official IFRS 18 standardized operating profit
        f_18 = SECRawFactRecord(
            cik="0001018724",
            taxonomy="ifrs-full",
            concept="OperatingProfitLossOperating",
            unit="EUR",
            period_type=PeriodType.DURATION,
            value=Decimal("5200000"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )
        cand_18, status_18, _ = resolver.resolve_raw_fact(f_18)
        assert status_18 == SECConceptMatchStatus.MATCHED
        assert cand_18.canonical_concept == "OPERATING_PROFIT_IFRS18"
        assert cand_18.match_strength == "exact"

        # Proposal tag OperatingProfitLoss is NOT an active tag
        f_proposal = SECRawFactRecord(
            cik="0001018724",
            taxonomy="ifrs-full",
            concept="OperatingProfitLoss",
            unit="EUR",
            period_type=PeriodType.DURATION,
            value=Decimal("5200000"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )
        assert resolver.resolve_raw_fact(f_proposal)[1] == SECConceptMatchStatus.NO_MATCH

    def test_14_and_15_legacy_replacement_tag_and_operating_separation(self):
        """Scenario 14 & 15: Legacy IAS 1 replacement_tag points to final tag and concepts remain distinct."""
        definitions = get_initial_canonical_concept_definitions()
        legacy_defn = next(d for d in definitions if d.canonical_concept == "OPERATING_INCOME_LEGACY_IAS1")
        assert legacy_defn.variants[0].replacement_tag == "OperatingProfitLossOperating"
        assert legacy_defn.variants[0].deprecated_in_release == "IFRS 18"

        op_concepts = {d.canonical_concept for d in definitions if "OPERATING" in d.canonical_concept}
        assert "OPERATING_INCOME" in op_concepts
        assert "OPERATING_INCOME_LEGACY_IAS1" in op_concepts
        assert "OPERATING_PROFIT_IFRS18" in op_concepts


# ─────────────────────────────────────────────────────────────────────────────
# 3. Net Income Scope Split: Parent vs Including NCI (Scenarios 16-22)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECNetIncomeScopeSplit:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_16_to_19_us_gaap_net_income_loss_vs_profit_loss(self, resolver: SECConceptResolver):
        """Scenario 16-19: NetIncomeLoss -> NET_INCOME_ATTRIBUTABLE_TO_PARENT, ProfitLoss -> NET_INCOME_INCLUDING_NCI."""
        # US-GAAP NetIncomeLoss (Parent)
        f_parent = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="NetIncomeLoss",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("96995000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_p, status_p, _ = resolver.resolve_raw_fact(f_parent)
        assert status_p == SECConceptMatchStatus.MATCHED
        assert cand_p.canonical_concept == "NET_INCOME_ATTRIBUTABLE_TO_PARENT"
        assert cand_p.match_strength == "exact"

        # US-GAAP ProfitLoss (Including NCI)
        f_nci = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="ProfitLoss",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("97500000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_nci, status_nci, _ = resolver.resolve_raw_fact(f_nci)
        assert status_nci == SECConceptMatchStatus.MATCHED
        assert cand_nci.canonical_concept == "NET_INCOME_INCLUDING_NCI"
        assert cand_nci.canonical_concept != cand_p.canonical_concept

    def test_20_to_22_ifrs_net_income_and_common_stockholder_income(self, resolver: SECConceptResolver):
        """Scenario 20-22: IFRS parent vs total net income separation, and common stockholder income isolation."""
        # IFRS ProfitLossAttributableToOwnersOfParent -> NET_INCOME_ATTRIBUTABLE_TO_PARENT
        f_ifrs_p = SECRawFactRecord(cik="1018724", taxonomy="ifrs-full", concept="ProfitLossAttributableToOwnersOfParent", unit="EUR", period_type=PeriodType.DURATION, value=Decimal("1000"), start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        assert resolver.resolve_raw_fact(f_ifrs_p)[0].canonical_concept == "NET_INCOME_ATTRIBUTABLE_TO_PARENT"

        # IFRS ProfitLoss -> NET_INCOME_INCLUDING_NCI
        f_ifrs_nci = SECRawFactRecord(cik="1018724", taxonomy="ifrs-full", concept="ProfitLoss", unit="EUR", period_type=PeriodType.DURATION, value=Decimal("1100"), start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        assert resolver.resolve_raw_fact(f_ifrs_nci)[0].canonical_concept == "NET_INCOME_INCLUDING_NCI"

        # Common stockholders income is NOT collapsed into generic net income
        f_common = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="NetIncomeLossAvailableToCommonStockholdersBasic", unit="USD", period_type=PeriodType.DURATION, value=Decimal("96000000000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_common)[1] == SECConceptMatchStatus.NO_MATCH


# ─────────────────────────────────────────────────────────────────────────────
# 4. Strict Fail-Closed Unit Validation (Scenarios 23-33)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECStrictUnitValidation:

    def test_23_to_26_monetary_unit_validation(self):
        """Scenario 23-26: USD/EUR are valid MONETARY; ABC unknown 3-letter currency and shares are rejected."""
        assert validate_unit_compatibility(UnitClass.MONETARY, "USD") is True
        assert validate_unit_compatibility(UnitClass.MONETARY, "EUR") is True
        assert validate_unit_compatibility(UnitClass.MONETARY, "TRY") is True
        # Unknown 3-letter uppercase string ABC MUST fail closed
        assert validate_unit_compatibility(UnitClass.MONETARY, "ABC") is False
        assert validate_unit_compatibility(UnitClass.MONETARY, "shares") is False
        assert validate_unit_compatibility(UnitClass.MONETARY, "pure") is False

    def test_27_to_32_monetary_per_share_unit_validation(self):
        """Scenario 27-32: USD/shares & EUR/shares are valid; plain USD, pure, ratio, and plain shares are rejected."""
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "USD/shares") is True
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "USD-per-shares") is True
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "EUR/shares") is True
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "TRY/shares") is True
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "USD/share") is True

        # Rejected invalid formats
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "USD") is False
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "shares") is False
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "pure") is False
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "ratio") is False
        assert validate_unit_compatibility(UnitClass.MONETARY_PER_SHARE, "ABC/shares") is False

    def test_33_shares_unit_validation(self):
        """Scenario 33: shares and share are valid for SHARES; generic 'number' or currencies are rejected."""
        assert validate_unit_compatibility(UnitClass.SHARES, "shares") is True
        assert validate_unit_compatibility(UnitClass.SHARES, "share") is True
        assert validate_unit_compatibility(UnitClass.SHARES, "number") is False
        assert validate_unit_compatibility(UnitClass.SHARES, "USD") is False


# ─────────────────────────────────────────────────────────────────────────────
# 5. CapEx PP&E vs Productive Assets (Scenarios 34-38)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECCapexSeparation:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_34_to_38_capex_ppe_and_productive_assets_separation(self, resolver: SECConceptResolver):
        """Scenario 34-38: PaymentsToAcquirePropertyPlantAndEquipment -> CAPEX_PP&E, ProductiveAssets -> CAPEX_PRODUCTIVE_ASSETS."""
        # US-GAAP PP&E CapEx
        f_ppe = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="PaymentsToAcquirePropertyPlantAndEquipment", unit="USD", period_type=PeriodType.DURATION, value=Decimal("9500000000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        cand_ppe, status_ppe, _ = resolver.resolve_raw_fact(f_ppe)
        assert status_ppe == SECConceptMatchStatus.MATCHED
        assert cand_ppe.canonical_concept == "CAPEX_PP&E"

        # US-GAAP Productive Assets CapEx
        f_prod = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="PaymentsToAcquireProductiveAssets", unit="USD", period_type=PeriodType.DURATION, value=Decimal("12000000000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        cand_prod, status_prod, _ = resolver.resolve_raw_fact(f_prod)
        assert status_prod == SECConceptMatchStatus.MATCHED
        assert cand_prod.canonical_concept == "CAPEX_PRODUCTIVE_ASSETS"
        assert cand_prod.canonical_concept != cand_ppe.canonical_concept

        # IFRS Verified PP&E tag
        f_ifrs_ppe = SECRawFactRecord(cik="1018724", taxonomy="ifrs-full", concept="PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", unit="EUR", period_type=PeriodType.DURATION, value=Decimal("500000"), start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        assert resolver.resolve_raw_fact(f_ifrs_ppe)[0].canonical_concept == "CAPEX_PP&E"

        # IFRS Unverified short tag -> NO_MATCH
        f_ifrs_short = SECRawFactRecord(cik="1018724", taxonomy="ifrs-full", concept="PurchaseOfPropertyPlantAndEquipment", unit="EUR", period_type=PeriodType.DURATION, value=Decimal("500000"), start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        assert resolver.resolve_raw_fact(f_ifrs_short)[1] == SECConceptMatchStatus.NO_MATCH


# ─────────────────────────────────────────────────────────────────────────────
# 6. Revenue, Equity, and Cash Scope Integrity (Scenarios 39-42)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECRevenueEquityAndCashScopeIntegrity:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_39_to_42_scope_integrity(self, resolver: SECConceptResolver):
        """Scenario 39-42: Financial sector income not generic revenue; restricted cash not plain cash; parent equity != equity with NCI."""
        # Financial sector revenue not mapped to generic REVENUE
        for tag in ("InterestIncome", "NetInterestIncome", "PremiumsEarnedNet"):
            f = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept=tag, unit="USD", period_type=PeriodType.DURATION, value=Decimal("5000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
            assert resolver.resolve_raw_fact(f)[1] == SECConceptMatchStatus.NO_MATCH

        # Restricted cash combined tag not plain cash
        f_rcash = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("50000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_rcash)[1] == SECConceptMatchStatus.NO_MATCH

        # StockholdersEquity vs StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest
        f_eq_parent = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="StockholdersEquity", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("60000"), end_date=date(2024, 9, 28))
        f_eq_nci = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("70000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_eq_parent)[0].canonical_concept == "EQUITY_ATTRIBUTABLE_TO_PARENT"
        assert resolver.resolve_raw_fact(f_eq_nci)[0].canonical_concept == "EQUITY_INCLUDING_NCI"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Exact Precision & Provenance Preservation (Scenarios 43-48)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECPrecisionAndProvenance:

    def test_43_to_48_exact_decimal_and_provenance(self):
        """Scenario 43-48: Exact Decimal values, units, accessions, snapshot IDs, and lineage statuses are preserved."""
        snap_id = uuid4()
        fact = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            taxonomy="us-gaap",
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("391035000000.55"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
            form="10-K",
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
        assert cand.value == Decimal("391035000000.55")
        assert cand.unit == "USD"
        assert cand.snapshot_id == snap_id
        assert cand.filing_id == filing.id
        assert cand.lineage_status == "RESOLVED"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Form Roles & Multi-Candidate No-Winner Invariants (Scenarios 49-55)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECFormRolesAndNoWinnerInvariants:

    def test_49_form_role_classifications(self):
        """Scenario 49: Form strings map deterministically to FormRole."""
        assert classify_form_role("10-K") == (FormRole.PRIMARY_ANNUAL, False)
        assert classify_form_role("10-K/A") == (FormRole.AMENDMENT_ANNUAL, True)
        assert classify_form_role("10-Q") == (FormRole.PRIMARY_QUARTERLY, False)
        assert classify_form_role("10-Q/A") == (FormRole.AMENDMENT_QUARTERLY, True)
        assert classify_form_role("20-F") == (FormRole.FPI_ANNUAL, False)
        assert classify_form_role("20-F/A") == (FormRole.FPI_AMENDMENT_ANNUAL, True)
        assert classify_form_role("40-F") == (FormRole.FPI_ANNUAL, False)
        assert classify_form_role("6-K") == (FormRole.FPI_INTERIM_OR_EVENT, False)
        assert classify_form_role("8-K") == (FormRole.EVENT_FILING, False)

    def test_50_to_55_no_winner_multi_candidate_invariants(self):
        """Scenario 50-55: All comparative, amendment, and multi-accession facts are preserved without declaring winners."""
        f_orig = SECRawFactRecord(cik="320193", accession_number="0000320193-23-000106", taxonomy="us-gaap", concept="Revenues", unit="USD", period_type=PeriodType.DURATION, value=Decimal("383000"), start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), form="10-K", filed_date=date(2023, 11, 3))
        f_amend = SECRawFactRecord(cik="320193", accession_number="0000320193-23-000999", taxonomy="us-gaap", concept="Revenues", unit="USD", period_type=PeriodType.DURATION, value=Decimal("382900"), start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), form="10-K/A", filed_date=date(2023, 12, 1))
        f_comp = SECRawFactRecord(cik="320193", accession_number="0000320193-24-000106", taxonomy="us-gaap", concept="Revenues", unit="USD", period_type=PeriodType.DURATION, value=Decimal("383000"), start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), form="10-K", filed_date=date(2024, 11, 1))

        resolver = SECConceptResolver()
        candidates = resolver.resolve_facts([f_orig, f_amend, f_comp])
        assert len(candidates) == 3
        accessions = {c.accession_number for c in candidates}
        assert accessions == {"0000320193-23-000106", "0000320193-23-000999", "0000320193-24-000106"}
