"""
backend/engine/private/sec/concepts.py
========================================
Canonical Financial Concept Definitions & Authoritative Taxonomy Variants Registry.

Core Invariants:
    - Pure economic concept definitions (Statement Family, Expected PeriodType, Expected UnitClass).
    - Authoritative taxonomy variant mappings (US-GAAP 2026, SEC-supported IFRS 2025, DEI).
    - Exact vs Compatible vs Legacy distinction preserved.
    - Net Income is split into NET_INCOME_ATTRIBUTABLE_TO_PARENT (NetIncomeLoss / ProfitLossAttributableToOwnersOfParent)
      and NET_INCOME_INCLUDING_NCI (ProfitLoss) to prevent ambiguous semantic collapse.
    - IFRS 18 Operating Profit (OPERATING_PROFIT_IFRS18 -> ifrs-full:OperatingProfitLossOperating) is strictly
      separated from legacy IAS 1 entity-specific subtotal (OPERATING_INCOME_LEGACY_IAS1 -> ifrs-full:ProfitLossFromOperatingActivities).
    - Physical PP&E CapEx (CAPEX_PP&E) is strictly separated from broader productive assets CapEx (CAPEX_PRODUCTIVE_ASSETS).
    - SEC-supported IFRS release is explicitly 2025; no fabricated SEC IFRS 2026 support.
    - No financial metric calculations (TTM, FCF, ROIC, Margins, Winner Selection).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from backend.engine.private.sec.models import PeriodType


class UnitClass(Enum):
    """Broad classification of expected measurement units."""
    MONETARY = "monetary"                     # USD, EUR, TRY, GBP, etc.
    SHARES = "shares"                         # shares, share
    MONETARY_PER_SHARE = "monetary_per_share" # USD/shares, USD-per-shares, EUR/shares, etc.
    PURE = "pure"                             # pure, ratio, percentage


class MatchStrength(Enum):
    """Semantic alignment strength of the taxonomy tag to the canonical concept."""
    EXACT = "exact"                         # 1-to-1 standard modern concept
    COMPATIBLE = "compatible"               # Highly compatible broad/narrow representation
    LEGACY_COMPATIBLE = "legacy_compatible" # Historical standard concept superseded by modern guidance


class StatementFamily(Enum):
    """Primary financial statement family."""
    INCOME_STATEMENT = "income_statement"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW_STATEMENT = "cash_flow_statement"
    SHARE_DATA = "share_data"


class VerificationStatus(Enum):
    """Verification provenance of the taxonomy mapping."""
    VERIFIED_OFFICIAL = "verified_official" # Validated against FASB / SEC / IASB documentation
    PROVISIONAL = "provisional"
    UNVERIFIED = "unverified"


class FormRole(Enum):
    """Deterministic classification of filing form roles without selecting winners."""
    PRIMARY_ANNUAL = "primary_annual"                 # 10-K
    AMENDMENT_ANNUAL = "amendment_annual"             # 10-K/A
    PRIMARY_QUARTERLY = "primary_quarterly"           # 10-Q
    AMENDMENT_QUARTERLY = "amendment_quarterly"       # 10-Q/A
    FPI_ANNUAL = "fpi_annual"                         # 20-F, 40-F
    FPI_AMENDMENT_ANNUAL = "fpi_amendment_annual"     # 20-F/A, 40-F/A
    FPI_INTERIM_OR_EVENT = "fpi_interim_or_event"     # 6-K
    EVENT_FILING = "event_filing"                     # 8-K
    OTHER = "other"


class SECConceptMatchStatus(Enum):
    """Resolution diagnostic status for raw XBRL facts."""
    MATCHED = "matched"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    INVALID_PERIOD_TYPE = "invalid_period_type"
    INVALID_UNIT = "invalid_unit"
    UNVERIFIED_VARIANT = "unverified_variant"


@dataclass(frozen=True)
class ConceptVariant:
    """
    Individual taxonomy tag variant mapped to a canonical concept.
    """
    taxonomy: str                           # us-gaap, ifrs-full, dei, srt
    tag: str                                # e.g. RevenueFromContractWithCustomerExcludingAssessedTax
    match_strength: MatchStrength           # EXACT, COMPATIBLE, LEGACY_COMPATIBLE
    priority: int                           # 1 = primary recommended, 2 = legacy/fallback
    semantic_scope: str                     # Exact economic definition notes
    verified_taxonomy_family: str           # "US-GAAP", "IFRS", "SEC-DEI"
    verified_taxonomy_release: str          # "2026", "SEC-supported IFRS 2025", "2020-2026", etc.
    verification_source: str                # Authoritative FASB / SEC / IASB citation
    verification_status: VerificationStatus = VerificationStatus.VERIFIED_OFFICIAL
    legacy_status: bool = False
    deprecated_in_release: Optional[str] = None
    replacement_tag: Optional[str] = None
    notes: Optional[str] = None
    active: bool = True

    def __post_init__(self) -> None:
        if self.active and self.verification_status != VerificationStatus.VERIFIED_OFFICIAL:
            raise ValueError(
                f"Variant {self.taxonomy}:{self.tag} cannot be ACTIVE without VERIFIED_OFFICIAL status."
            )
        if not self.verification_source.strip():
            raise ValueError(
                f"Variant {self.taxonomy}:{self.tag} requires an authoritative verification_source citation."
            )


@dataclass
class CanonicalSECConceptDefinition:
    """
    Canonical economic financial concept definition.
    """
    canonical_concept: str
    description: str
    statement_family: StatementFamily
    expected_period_type: PeriodType
    expected_unit_class: UnitClass
    variants: List[ConceptVariant] = field(default_factory=list)
    notes: Optional[str] = None
    active: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Authoritative Initial Canonical Concepts Registry (Phase 8B.1.6 Hardened)
# ─────────────────────────────────────────────────────────────────────────────

def get_initial_canonical_concept_definitions() -> List[CanonicalSECConceptDefinition]:
    """
    Constructs the verified baseline canonical financial concept definitions.
    Validated against:
        - FASB US-GAAP Taxonomy (2020-2026 releases; current SEC-supported = 2026)
        - SEC-Supported IFRS Taxonomy (2020-2025 releases; current SEC-supported = 2025)
        - SEC DEI Taxonomy (2020-2026 releases)
    """
    return [
        # 1. REVENUE
        CanonicalSECConceptDefinition(
            canonical_concept="REVENUE",
            description="Gross revenue from contracts with customers, goods sold, or services rendered.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="RevenueFromContractWithCustomerExcludingAssessedTax",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="FASB ASC 606 revenue from customer contracts excluding taxes.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 606-10-50-4(a))",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="Revenues",
                    match_strength=MatchStrength.COMPATIBLE,
                    priority=2,
                    semantic_scope="General top-level aggregate revenues across operating segments.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="SalesRevenueNet",
                    match_strength=MatchStrength.LEGACY_COMPATIBLE,
                    priority=3,
                    semantic_scope="Legacy net product/service revenue net of returns and discounts.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="Historical Pre-ASC 606",
                    verification_source="FASB US-GAAP Taxonomy Legacy Standard",
                    legacy_status=True,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Revenue",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS 15 revenue from ordinary activities.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.82(a), IFRS 15.113)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="RevenueFromContractsWithCustomers",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS 15 revenue from customer contracts.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IFRS 15.113)",
                    legacy_status=False,
                ),
            ],
        ),

        # 2. OPERATING_INCOME (US-GAAP Operating Income / Loss)
        CanonicalSECConceptDefinition(
            canonical_concept="OPERATING_INCOME",
            description="Operating income or loss before non-operating items, interest, and taxes under US-GAAP.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="OperatingIncomeLoss",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Operating profit or loss from core business operations under US-GAAP.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 220-10-S99-5)",
                    legacy_status=False,
                ),
            ],
        ),

        # 3. OPERATING_INCOME_LEGACY_IAS1 (IFRS IAS 1 Entity-Specific Operating Subtotal)
        CanonicalSECConceptDefinition(
            canonical_concept="OPERATING_INCOME_LEGACY_IAS1",
            description="Legacy IAS 1 operating profit or loss subtotal (entity-specific composition).",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="ProfitLossFromOperatingActivities",
                    match_strength=MatchStrength.LEGACY_COMPATIBLE,
                    priority=1,
                    semantic_scope="IAS 1 legacy operating profit/loss subtotal with entity-specific composition.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.BC56)",
                    legacy_status=True,
                    deprecated_in_release="IFRS 18",
                    replacement_tag="OperatingProfitLossOperating",
                    notes="Deprecated by IFRS 18; composition is entity-specific and distinct from IFRS 18 standardized operating profit.",
                ),
            ],
        ),

        # 4. OPERATING_PROFIT_IFRS18 (IFRS 18 Standardized Operating Profit / Loss)
        CanonicalSECConceptDefinition(
            canonical_concept="OPERATING_PROFIT_IFRS18",
            description="IFRS 18 standardized operating profit or loss subtotal.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="OperatingProfitLossOperating",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Standardized operating profit or loss defined by IFRS 18.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2025",
                    verification_source="IASB IFRS 18 / SEC-supported IFRS 2025 Taxonomy",
                    legacy_status=False,
                    notes="Standardized IFRS 18 category; not interchangeable with legacy IAS 1 ProfitLossFromOperatingActivities.",
                ),
            ],
        ),

        # 5. NET_INCOME_ATTRIBUTABLE_TO_PARENT (Net Income Attributable to Parent Common Stockholders)
        CanonicalSECConceptDefinition(
            canonical_concept="NET_INCOME_ATTRIBUTABLE_TO_PARENT",
            description="Net income or loss attributable exclusively to parent entity stockholders / owners of the parent.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="NetIncomeLoss",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="FASB ASC 220 net income or loss attributable to parent entity stockholders.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 220-10-S99-5)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="ProfitLossAttributableToOwnersOfParent",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS profit or loss attributable to owners of the parent.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.81B(a)(ii))",
                    legacy_status=False,
                ),
            ],
        ),

        # 6. NET_INCOME_INCLUDING_NCI (Consolidated Total Net Income / ProfitLoss before NCI Allocation)
        CanonicalSECConceptDefinition(
            canonical_concept="NET_INCOME_INCLUDING_NCI",
            description="Consolidated total profit or loss for the period before allocation to non-controlling interests.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="ProfitLoss",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Consolidated profit or loss for the period before noncontrolling interest attribution.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 220-10-55)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="ProfitLoss",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS total consolidated profit or loss for the period before attribution.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.81A(a))",
                    legacy_status=False,
                ),
            ],
        ),

        # 7. TOTAL_ASSETS
        CanonicalSECConceptDefinition(
            canonical_concept="TOTAL_ASSETS",
            description="Total carrying amount of all recognized economic assets.",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="Assets",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Sum of current and non-current balance sheet assets.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Assets",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total carrying amount of IFRS balance sheet assets.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.66)",
                    legacy_status=False,
                ),
            ],
        ),

        # 8. TOTAL_LIABILITIES
        CanonicalSECConceptDefinition(
            canonical_concept="TOTAL_LIABILITIES",
            description="Total obligations and carrying amount of all balance sheet liabilities.",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="Liabilities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Sum of all current and long-term liabilities.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Liabilities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total carrying amount of IFRS balance sheet liabilities.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.69)",
                    legacy_status=False,
                ),
            ],
        ),

        # 9. EQUITY_ATTRIBUTABLE_TO_PARENT
        CanonicalSECConceptDefinition(
            canonical_concept="EQUITY_ATTRIBUTABLE_TO_PARENT",
            description="Stockholders' equity attributable exclusively to the parent entity stockholders.",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="StockholdersEquity",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Equity of the parent company stockholders excluding non-controlling interests.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="EquityAttributableToOwnersOfParent",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS equity attributable to owners of the parent.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.54(q))",
                    legacy_status=False,
                ),
            ],
        ),

        # 10. EQUITY_INCLUDING_NCI
        CanonicalSECConceptDefinition(
            canonical_concept="EQUITY_INCLUDING_NCI",
            description="Total equity including portion attributable to non-controlling interests.",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total stockholders equity plus noncontrolling interest portion.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 810-10-45-15)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Equity",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total IFRS balance sheet equity including non-controlling interest.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.54(r))",
                    legacy_status=False,
                ),
            ],
        ),

        # 11. CURRENT_ASSETS
        CanonicalSECConceptDefinition(
            canonical_concept="CURRENT_ASSETS",
            description="Total assets expected to be converted to cash or consumed within one year or operating cycle.",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="AssetsCurrent",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Sum of current assets expected to realize within normal operating cycle.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-45-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CurrentAssets",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total IFRS current assets.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.66)",
                    legacy_status=False,
                ),
            ],
        ),

        # 12. CURRENT_LIABILITIES
        CanonicalSECConceptDefinition(
            canonical_concept="CURRENT_LIABILITIES",
            description="Total obligations expected to be settled within one year or operating cycle.",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="LiabilitiesCurrent",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Sum of current liabilities due within normal operating cycle.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-45-6)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CurrentLiabilities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total IFRS current liabilities.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.69)",
                    legacy_status=False,
                ),
            ],
        ),

        # 13. CASH_AND_CASH_EQUIVALENTS
        CanonicalSECConceptDefinition(
            canonical_concept="CASH_AND_CASH_EQUIVALENTS",
            description="Cash, bank deposits, and highly liquid short-term investments.",
            statement_family=StatementFamily.BALANCE_SHEET,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="CashAndCashEquivalentsAtCarryingValue",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Cash on hand and short term highly liquid investments.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="Cash",
                    match_strength=MatchStrength.COMPATIBLE,
                    priority=2,
                    semantic_scope="Unrestricted cash balance.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CashAndCashEquivalents",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS cash and cash equivalents.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 7.6, IAS 1.54(i))",
                    legacy_status=False,
                ),
            ],
        ),

        # 14. OPERATING_CASH_FLOW
        CanonicalSECConceptDefinition(
            canonical_concept="OPERATING_CASH_FLOW",
            description="Net cash flow provided by or used in operating activities (CFO).",
            statement_family=StatementFamily.CASH_FLOW_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="NetCashProvidedByUsedInOperatingActivities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Operating cash inflows and outflows from operating activities.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 230-10-45-24)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CashFlowsFromUsedInOperatingActivities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS net cash flow from operating activities.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 7.10)",
                    legacy_status=False,
                ),
            ],
        ),

        # 15. CAPEX_PP&E (Physical Property, Plant, and Equipment Additions Only)
        CanonicalSECConceptDefinition(
            canonical_concept="CAPEX_PP&E",
            description="Cash payments made strictly to acquire physical property, plant, and equipment.",
            statement_family=StatementFamily.CASH_FLOW_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="PaymentsToAcquirePropertyPlantAndEquipment",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Cash outflow for purchases of physical property, plant, and equipment.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 230-10-45-13(c))",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS investing cash flows for physical PP&E purchases.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 7.16(a))",
                    legacy_status=False,
                ),
            ],
        ),

        # 16. CAPEX_PRODUCTIVE_ASSETS (Broader Capital Expenditures including Intangibles)
        CanonicalSECConceptDefinition(
            canonical_concept="CAPEX_PRODUCTIVE_ASSETS",
            description="Broader capital expenditures for productive assets including PP&E, software, and intangibles.",
            statement_family=StatementFamily.CASH_FLOW_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="PaymentsToAcquireProductiveAssets",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Broader cash outflow for productive physical and intangible capital assets.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 230-10-45-13)",
                    legacy_status=False,
                    notes="Broader than physical PP&E; must not be combined with CAPEX_PP&E to avoid double counting.",
                ),
            ],
        ),

        # 17. DILUTED_EPS
        CanonicalSECConceptDefinition(
            canonical_concept="DILUTED_EPS",
            description="Diluted earnings per share available to common stockholders.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY_PER_SHARE,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="EarningsPerShareDiluted",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Net income per diluted common share.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 260-10-45-2)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="DilutedEarningsLossPerShare",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS diluted earnings per share.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 33.66)",
                    legacy_status=False,
                ),
            ],
        ),

        # 18. DILUTED_WEIGHTED_AVERAGE_SHARES
        CanonicalSECConceptDefinition(
            canonical_concept="DILUTED_WEIGHTED_AVERAGE_SHARES",
            description="Weighted-average number of common shares outstanding including dilutive potential shares.",
            statement_family=StatementFamily.SHARE_DATA,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.SHARES,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="WeightedAverageNumberOfDilutedSharesOutstanding",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Diluted weighted average shares used in EPS calculation.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 260-10-50-1(a))",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="AdjustedWeightedAverageShares",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS diluted weighted average shares.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 33.70(b))",
                    legacy_status=False,
                ),
            ],
        ),

        # 19. SHARES_OUTSTANDING
        CanonicalSECConceptDefinition(
            canonical_concept="SHARES_OUTSTANDING",
            description="Total number of common shares outstanding at a specific point in time.",
            statement_family=StatementFamily.SHARE_DATA,
            expected_period_type=PeriodType.INSTANT,
            expected_unit_class=UnitClass.SHARES,
            variants=[
                ConceptVariant(
                    taxonomy="dei",
                    tag="EntityCommonStockSharesOutstanding",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="SEC Document and Entity Information common stock shares outstanding on cover page.",
                    verified_taxonomy_family="SEC-DEI",
                    verified_taxonomy_release="2020-2026",
                    verification_source="SEC DEI Taxonomy 2020-2026",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="CommonStockSharesOutstanding",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Balance sheet common shares issued and outstanding.",
                    verified_taxonomy_family="US-GAAP",
                    verified_taxonomy_release="2020-2026",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="NumberOfSharesOutstanding",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS number of shares outstanding.",
                    verified_taxonomy_family="IFRS",
                    verified_taxonomy_release="SEC-supported IFRS 2020-2025",
                    verification_source="IASB IFRS Taxonomy (IAS 1.79(a)(iv))",
                    legacy_status=False,
                ),
            ],
        ),
    ]
