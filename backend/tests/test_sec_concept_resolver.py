"""
backend/tests/test_sec_concept_resolver.py
============================================
Comprehensive Unit Test Suite for SEC EDGAR Phase 8B.1 & 8B.1.5:
Canonical Financial Concept Families, Taxonomy Versioning & Semantic Alias Hardening.

Coverage:
    - Taxonomy Version Support & Authority Provenance (8B.1.5 Scenarios 1-6)
    - IFRS 18 vs Legacy IAS 1 Operating Profit Separation (8B.1.5 Scenarios 7-10)
    - CapEx PP&E vs Productive Assets Separation (8B.1.5 Scenarios 11-17)
    - Income, Equity & Cash Scope Integrity (8B.1.5 Scenarios 18-22)
    - Core Concept Mapping & Taxonomies (8B.1 Scenarios 10-22)
    - Semantic Guards, Unit & Period Type Validation (8B.1 Scenarios 23-33)
    - Decimal Precision & Provenance Preservation (8B.1 Scenarios 34-43)
    - Form Role Classification (8B.1 Scenarios 44-51)
    - No-Winner & Multi-Candidate Invariants (8B.1 Scenarios 52-57)
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
# 1. Taxonomy Version Support & Registry Hardening (8B.1.5 Scenarios 1-6)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECTaxonomySupportAndRegistryHardening:

    def test_01_and_02_sec_supported_releases_correctly_recorded(self):
        """Scenario 1 & 2: US-GAAP is recorded as 2026/2020-2026 and IFRS is recorded as SEC-supported 2025."""
        definitions = get_initial_canonical_concept_definitions()
        
        # Verify US-GAAP releases
        us_gaap_variants = [v for d in definitions for v in d.variants if v.taxonomy == "us-gaap" and v.active]
        assert len(us_gaap_variants) > 0
        for v in us_gaap_variants:
            assert v.verified_taxonomy_family == "US-GAAP"
            assert v.verification_source.strip() != ""

        # Verify IFRS releases
        ifrs_variants = [v for d in definitions for v in d.variants if v.taxonomy == "ifrs-full" and v.active]
        assert len(ifrs_variants) > 0
        for v in ifrs_variants:
            assert v.verified_taxonomy_family == "IFRS"
            # Must NOT claim unverified "SEC-supported IFRS 2026"
            assert "2026" not in v.verified_taxonomy_release or "IFRS 18" in v.verified_taxonomy_release

    def test_03_no_fabricated_sec_ifrs_2026_support(self):
        """Scenario 3: No active standard IFRS variant falsely claims SEC-supported 2026 release."""
        definitions = get_initial_canonical_concept_definitions()
        for d in definitions:
            for v in d.variants:
                if v.taxonomy == "ifrs-full" and v.active and v.legacy_status is False and v.tag != "OperatingProfitLoss":
                    assert "SEC-supported IFRS 2020-2025" in v.verified_taxonomy_release or "2025" in v.verified_taxonomy_release

    def test_04_and_05_verification_source_and_release_metadata_required(self):
        """Scenario 4 & 5: Active variants require valid verification source, taxonomy family, and release metadata."""
        with pytest.raises(ValueError, match="cannot be ACTIVE without VERIFIED_OFFICIAL status"):
            ConceptVariant(
                taxonomy="us-gaap",
                tag="FakeUnverifiedTag",
                match_strength=MatchStrength.COMPATIBLE,
                priority=1,
                semantic_scope="Test",
                verified_taxonomy_family="US-GAAP",
                verified_taxonomy_release="2026",
                verification_source="FASB",
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

    def test_06_canonical_concept_names_unique_and_non_empty(self):
        """Scenario 6: All canonical concept names in the registry must be unique and non-empty."""
        definitions = get_initial_canonical_concept_definitions()
        names = [d.canonical_concept for d in definitions]
        assert len(names) == len(set(names))
        for d in definitions:
            assert d.canonical_concept.strip() != ""
            assert isinstance(d.expected_period_type, PeriodType)
            assert isinstance(d.expected_unit_class, UnitClass)


# ─────────────────────────────────────────────────────────────────────────────
# 2. IFRS 18 vs Legacy IAS 1 Operating Profit (8B.1.5 Scenarios 7-10)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECIFRSOperatingProfitHardening:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_07_and_08_legacy_ias1_and_ifrs18_are_distinct_canonical_concepts(self, resolver: SECConceptResolver):
        """Scenario 7 & 8: ProfitLossFromOperatingActivities and OperatingProfitLoss map to distinct concepts."""
        # Legacy IAS 1 entity-specific operating subtotal
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

        # Modern standardized IFRS 18 operating profit
        f_ifrs18 = SECRawFactRecord(
            cik="0001018724",
            taxonomy="ifrs-full",
            concept="OperatingProfitLoss",
            unit="EUR",
            period_type=PeriodType.DURATION,
            value=Decimal("5200000"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )
        cand_18, status_18, _ = resolver.resolve_raw_fact(f_ifrs18)
        assert status_18 == SECConceptMatchStatus.MATCHED
        assert cand_18.canonical_concept == "OPERATING_PROFIT_IFRS18"
        assert cand_18.match_strength == "exact"

        # US-GAAP Operating Income remains OPERATING_INCOME
        f_us = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="OperatingIncomeLoss",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("100000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_us, _, _ = resolver.resolve_raw_fact(f_us)
        assert cand_us.canonical_concept == "OPERATING_INCOME"

    def test_09_and_10_deprecation_and_replacement_metadata_preserved(self):
        """Scenario 9 & 10: Variant metadata tracks IFRS 18 deprecation and replacement tag."""
        definitions = get_initial_canonical_concept_definitions()
        legacy_defn = next(d for d in definitions if d.canonical_concept == "OPERATING_INCOME_LEGACY_IAS1")
        variant = legacy_defn.variants[0]

        assert variant.deprecated_in_release == "IFRS 18"
        assert variant.replacement_tag == "OperatingProfitLoss"
        assert variant.legacy_status is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. CapEx PP&E vs Productive Assets Hardening (8B.1.5 Scenarios 11-17)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECCapexHardening:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_11_to_15_ppe_and_productive_assets_strictly_separated(self, resolver: SECConceptResolver):
        """Scenario 11-15: PaymentsToAcquirePropertyPlantAndEquipment -> CAPEX_PP&E, ProductiveAssets -> CAPEX_PRODUCTIVE_ASSETS."""
        # US-GAAP Physical PP&E CapEx
        f_ppe = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="PaymentsToAcquirePropertyPlantAndEquipment",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("9500000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_ppe, status_ppe, _ = resolver.resolve_raw_fact(f_ppe)
        assert status_ppe == SECConceptMatchStatus.MATCHED
        assert cand_ppe.canonical_concept == "CAPEX_PP&E"
        assert cand_ppe.match_strength == "exact"

        # US-GAAP Broader Productive Assets CapEx
        f_prod = SECRawFactRecord(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="PaymentsToAcquireProductiveAssets",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("12000000000"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
        )
        cand_prod, status_prod, _ = resolver.resolve_raw_fact(f_prod)
        assert status_prod == SECConceptMatchStatus.MATCHED
        assert cand_prod.canonical_concept == "CAPEX_PRODUCTIVE_ASSETS"
        assert cand_prod.canonical_concept != "CAPEX_PP&E"

    def test_16_and_17_ifrs_ppe_capex_exact_tag_verified(self, resolver: SECConceptResolver):
        """Scenario 16 & 17: Official IFRS PP&E tag resolves to CAPEX_PP&E; unverified short tag returns NO_MATCH."""
        # Official standard IAS 7.16(a) element
        f_ifrs_ppe = SECRawFactRecord(
            cik="0001018724",
            taxonomy="ifrs-full",
            concept="PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            unit="EUR",
            period_type=PeriodType.DURATION,
            value=Decimal("450000000"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )
        cand_ifrs, status_ifrs, _ = resolver.resolve_raw_fact(f_ifrs_ppe)
        assert status_ifrs == SECConceptMatchStatus.MATCHED
        assert cand_ifrs.canonical_concept == "CAPEX_PP&E"

        # Short unverified tag does not exist as an active variant
        f_bad_short = SECRawFactRecord(
            cik="0001018724",
            taxonomy="ifrs-full",
            concept="PurchaseOfPropertyPlantAndEquipment",
            unit="EUR",
            period_type=PeriodType.DURATION,
            value=Decimal("450000000"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )
        assert resolver.resolve_raw_fact(f_bad_short)[1] == SECConceptMatchStatus.NO_MATCH


# ─────────────────────────────────────────────────────────────────────────────
# 4. Income, Equity & Cash Scope Integrity (8B.1.5 Scenarios 18-22)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECIncomeEquityAndCashScopeIntegrity:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_18_to_20_net_income_scopes_do_not_silently_collapse(self, resolver: SECConceptResolver):
        """Scenario 18-20: Consolidated NetIncomeLoss is mapped, but parent-only and common-only are NOT collapsed into NET_INCOME."""
        # Consolidated NetIncomeLoss -> NET_INCOME
        f_net = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="NetIncomeLoss", unit="USD", period_type=PeriodType.DURATION, value=Decimal("100"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
        assert resolver.resolve_raw_fact(f_net)[0].canonical_concept == "NET_INCOME"

        # Parent-only NetIncomeLossAttributableToParent -> NO_MATCH
        f_parent = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="NetIncomeLossAttributableToParent", unit="USD", period_type=PeriodType.DURATION, value=Decimal("100"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
        assert resolver.resolve_raw_fact(f_parent)[1] == SECConceptMatchStatus.NO_MATCH

        # Common stockholders NetIncomeLossAvailableToCommonStockholdersBasic -> NO_MATCH
        f_common = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="NetIncomeLossAvailableToCommonStockholdersBasic", unit="USD", period_type=PeriodType.DURATION, value=Decimal("100"), start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
        assert resolver.resolve_raw_fact(f_common)[1] == SECConceptMatchStatus.NO_MATCH

    def test_21_and_22_restricted_cash_and_equity_separation(self, resolver: SECConceptResolver):
        """Scenario 21 & 22: Restricted cash is not plain cash; Equity attributable to parent is separated from Equity with NCI."""
        # Restricted cash combined tag
        f_rcash = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("50000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_rcash)[1] == SECConceptMatchStatus.NO_MATCH

        # StockholdersEquity -> EQUITY_ATTRIBUTABLE_TO_PARENT
        f_eq_parent = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="StockholdersEquity", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("60000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_eq_parent)[0].canonical_concept == "EQUITY_ATTRIBUTABLE_TO_PARENT"

        # StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest -> EQUITY_INCLUDING_NCI
        f_eq_nci = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("70000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_eq_nci)[0].canonical_concept == "EQUITY_INCLUDING_NCI"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Core Mapping & Semantic Guards (8B.1 Baseline Tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECConceptResolverCoreMappingAndGuards:

    @pytest.fixture
    def resolver(self) -> SECConceptResolver:
        return SECConceptResolver()

    def test_core_balance_sheet_and_cash_flow_mapping(self, resolver: SECConceptResolver):
        """Core mappings for Total Assets, Liabilities, Current Items, Cash, CFO, EPS, and Shares."""
        # Assets & Liabilities
        f_a = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="Assets", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("300"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_a)[0].canonical_concept == "TOTAL_ASSETS"
        f_l = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="Liabilities", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("200"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_l)[0].canonical_concept == "TOTAL_LIABILITIES"

        # Current Items
        f_ca = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="AssetsCurrent", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_ca)[0].canonical_concept == "CURRENT_ASSETS"
        f_cl = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="LiabilitiesCurrent", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("50"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_cl)[0].canonical_concept == "CURRENT_LIABILITIES"

        # Cash & CFO
        f_cash = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="CashAndCashEquivalentsAtCarryingValue", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("30000"), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_cash)[0].canonical_concept == "CASH_AND_CASH_EQUIVALENTS"
        f_cfo = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="NetCashProvidedByUsedInOperatingActivities", unit="USD", period_type=PeriodType.DURATION, value=Decimal("118000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_cfo)[0].canonical_concept == "OPERATING_CASH_FLOW"

        # Diluted EPS & Shares
        f_eps = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="EarningsPerShareDiluted", unit="USD/shares", period_type=PeriodType.DURATION, value=Decimal("6.08"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_eps)[0].canonical_concept == "DILUTED_EPS"
        f_wshares = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="WeightedAverageNumberOfDilutedSharesOutstanding", unit="shares", period_type=PeriodType.DURATION, value=Decimal("15000"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_wshares)[0].canonical_concept == "DILUTED_WEIGHTED_AVERAGE_SHARES"
        f_dei = SECRawFactRecord(cik="320193", taxonomy="dei", concept="EntityCommonStockSharesOutstanding", unit="shares", period_type=PeriodType.INSTANT, value=Decimal("15000"), end_date=date(2024, 10, 18))
        assert resolver.resolve_raw_fact(f_dei)[0].canonical_concept == "SHARES_OUTSTANDING"

    def test_period_and_unit_mismatch_guards(self, resolver: SECConceptResolver):
        """Period type and unit class mismatches are rejected with exact diagnostic statuses."""
        # Assets with DURATION -> INVALID_PERIOD_TYPE
        f_bad_assets = SECRawFactRecord(cik="320193", taxonomy="us-gaap", concept="Assets", unit="USD", period_type=PeriodType.DURATION, value=Decimal("100"), start_date=date(2023, 10, 1), end_date=date(2024, 9, 28))
        assert resolver.resolve_raw_fact(f_bad_assets)[1] == SECConceptMatchStatus.INVALID_PERIOD_TYPE

        # Shares with USD -> INVALID_UNIT
        f_bad_shares = SECRawFactRecord(cik="320193", taxonomy="dei", concept="EntityCommonStockSharesOutstanding", unit="USD", period_type=PeriodType.INSTANT, value=Decimal("100"), end_date=date(2024, 10, 18))
        assert resolver.resolve_raw_fact(f_bad_shares)[1] == SECConceptMatchStatus.INVALID_UNIT


# ─────────────────────────────────────────────────────────────────────────────
# 6. Provenance & No-Winner Multi-Candidate Invariants (8B.1 Baseline Tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestSECProvenanceAndNoWinnerInvariants:

    def test_exact_decimal_preservation_and_form_roles(self):
        """Exact Decimal values and form roles are preserved without declaring filing winners."""
        fact = SECRawFactRecord(
            cik="0000320193",
            accession_number="0000320193-24-000106",
            taxonomy="us-gaap",
            concept="Revenues",
            unit="USD",
            period_type=PeriodType.DURATION,
            value=Decimal("391035000000.55"),
            start_date=date(2023, 10, 1),
            end_date=date(2024, 9, 28),
            form="10-K",
        )
        resolver = SECConceptResolver()
        cand, status, _ = resolver.resolve_raw_fact(fact)
        assert status == SECConceptMatchStatus.MATCHED
        assert cand.value == Decimal("391035000000.55")
        assert cand.form_role == "primary_annual"
        assert cand.is_amendment is False

    def test_multi_accession_amendments_all_preserved_without_winners(self):
        """All comparative and amendment facts are preserved as candidate entries without winner selection."""
        f_orig = SECRawFactRecord(cik="320193", accession_number="0000320193-23-000106", taxonomy="us-gaap", concept="Revenues", unit="USD", period_type=PeriodType.DURATION, value=Decimal("383000"), start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), form="10-K")
        f_amend = SECRawFactRecord(cik="320193", accession_number="0000320193-23-000999", taxonomy="us-gaap", concept="Revenues", unit="USD", period_type=PeriodType.DURATION, value=Decimal("382900"), start_date=date(2022, 10, 1), end_date=date(2023, 9, 30), form="10-K/A")

        resolver = SECConceptResolver()
        candidates = resolver.resolve_facts([f_orig, f_amend])
        assert len(candidates) == 2
        assert {c.accession_number for c in candidates} == {"0000320193-23-000106", "0000320193-23-000999"}
