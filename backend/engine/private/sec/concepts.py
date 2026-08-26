"""
backend/engine/private/sec/concepts.py
========================================
Canonical Financial Concept Definitions & Authoritative Taxonomy Variants Registry.

Core Invariants:
    - Pure economic concept definitions (Statement Family, Expected PeriodType, Expected UnitClass).
    - Authoritative taxonomy variant mappings (US-GAAP, IFRS, DEI) with explicit verification sources.
    - Exact vs Compatible vs Legacy distinction preserved.
    - No fuzzy or ambiguous mappings.
    - Inactive / unverified variants are rejected at runtime.
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
    taxonomy: str                       # us-gaap, ifrs-full, dei, srt
    tag: str                            # e.g. RevenueFromContractWithCustomerExcludingAssessedTax
    match_strength: MatchStrength       # EXACT, COMPATIBLE, LEGACY_COMPATIBLE
    priority: int                       # 1 = primary recommended, 2 = legacy/fallback
    semantic_scope: str                 # Exact economic definition notes
    verification_source: str            # Authoritative FASB / SEC / IASB citation
    verification_status: VerificationStatus = VerificationStatus.VERIFIED_OFFICIAL
    legacy_status: bool = False
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
# Authoritative Initial Canonical Concepts Registry (Phase 8B.1)
# ─────────────────────────────────────────────────────────────────────────────

def get_initial_canonical_concept_definitions() -> List[CanonicalSECConceptDefinition]:
    """
    Constructs the verified baseline canonical financial concept definitions.
    Validated against FASB US-GAAP (2020-2026 releases) and IFRS standard taxonomies.
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
                    verification_source="FASB US-GAAP Taxonomy 2020-2026 (ASC 606-10-50-4(a))",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="Revenues",
                    match_strength=MatchStrength.COMPATIBLE,
                    priority=2,
                    semantic_scope="General top-level aggregate revenues across operating segments.",
                    verification_source="FASB US-GAAP Taxonomy 2020-2026",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="SalesRevenueNet",
                    match_strength=MatchStrength.LEGACY_COMPATIBLE,
                    priority=3,
                    semantic_scope="Legacy net product/service revenue net of returns and discounts.",
                    verification_source="FASB US-GAAP Taxonomy Legacy Standard",
                    legacy_status=True,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Revenue",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS 15 revenue from ordinary activities.",
                    verification_source="IASB IFRS Taxonomy 2020-2026 (IAS 1.82(a), IFRS 15.113)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="RevenueFromContractsWithCustomers",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS 15 revenue from customer contracts.",
                    verification_source="IASB IFRS Taxonomy 2020-2026 (IFRS 15.113)",
                    legacy_status=False,
                ),
            ],
        ),

        # 2. OPERATING_INCOME
        CanonicalSECConceptDefinition(
            canonical_concept="OPERATING_INCOME",
            description="Operating income or loss before non-operating items, interest, and taxes.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="OperatingIncomeLoss",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Operating profit or loss from core business operations.",
                    verification_source="FASB US-GAAP Taxonomy (ASC 220-10-S99-5)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="ProfitLossFromOperatingActivities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS operating profit or loss.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.BC56)",
                    legacy_status=False,
                ),
            ],
        ),

        # 3. NET_INCOME
        CanonicalSECConceptDefinition(
            canonical_concept="NET_INCOME",
            description="Consolidated net income or loss for the period.",
            statement_family=StatementFamily.INCOME_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="NetIncomeLoss",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Consolidated net profit or loss after taxes and expenses.",
                    verification_source="FASB US-GAAP Taxonomy (ASC 220-10-S99-5)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="ProfitLoss",
                    match_strength=MatchStrength.COMPATIBLE,
                    priority=2,
                    semantic_scope="Total comprehensive consolidated profit or loss for the period.",
                    verification_source="FASB US-GAAP Taxonomy (ASC 220-10-55)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="ProfitLoss",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS consolidated profit or loss for the period.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.81A(a))",
                    legacy_status=False,
                ),
            ],
        ),

        # 4. TOTAL_ASSETS
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Assets",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total carrying amount of IFRS balance sheet assets.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.66)",
                    legacy_status=False,
                ),
            ],
        ),

        # 5. TOTAL_LIABILITIES
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Liabilities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total carrying amount of IFRS balance sheet liabilities.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.69)",
                    legacy_status=False,
                ),
            ],
        ),

        # 6. EQUITY_ATTRIBUTABLE_TO_PARENT
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="EquityAttributableToOwnersOfParent",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS equity attributable to owners of the parent.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.54(q))",
                    legacy_status=False,
                ),
            ],
        ),

        # 7. EQUITY_INCLUDING_NCI
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 810-10-45-15)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="Equity",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total IFRS balance sheet equity including non-controlling interest.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.54(r))",
                    legacy_status=False,
                ),
            ],
        ),

        # 8. CURRENT_ASSETS
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-45-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CurrentAssets",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total IFRS current assets.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.66)",
                    legacy_status=False,
                ),
            ],
        ),

        # 9. CURRENT_LIABILITIES
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-45-6)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CurrentLiabilities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Total IFRS current liabilities.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.69)",
                    legacy_status=False,
                ),
            ],
        ),

        # 10. CASH_AND_CASH_EQUIVALENTS
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="Cash",
                    match_strength=MatchStrength.COMPATIBLE,
                    priority=2,
                    semantic_scope="Unrestricted cash balance.",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CashAndCashEquivalents",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS cash and cash equivalents.",
                    verification_source="IASB IFRS Taxonomy (IAS 7.6, IAS 1.54(i))",
                    legacy_status=False,
                ),
            ],
        ),

        # 11. OPERATING_CASH_FLOW
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 230-10-45-24)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="CashFlowsFromUsedInOperatingActivities",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS net cash flow from operating activities.",
                    verification_source="IASB IFRS Taxonomy (IAS 7.10)",
                    legacy_status=False,
                ),
            ],
        ),

        # 12. CAPEX_PP&E
        CanonicalSECConceptDefinition(
            canonical_concept="CAPEX_PP&E",
            description="Cash payments made to acquire property, plant, and equipment (capital expenditure).",
            statement_family=StatementFamily.CASH_FLOW_STATEMENT,
            expected_period_type=PeriodType.DURATION,
            expected_unit_class=UnitClass.MONETARY,
            variants=[
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="PaymentsToAcquirePropertyPlantAndEquipment",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Cash outflow for purchases of capital property, plant, and equipment.",
                    verification_source="FASB US-GAAP Taxonomy (ASC 230-10-45-13(c))",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="PaymentsToAcquireProductiveAssets",
                    match_strength=MatchStrength.COMPATIBLE,
                    priority=2,
                    semantic_scope="Broader cash outflow for productive physical capital assets.",
                    verification_source="FASB US-GAAP Taxonomy (ASC 230-10-45-13)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="PurchaseOfPropertyPlantAndEquipment",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS capital payments for PP&E.",
                    verification_source="IASB IFRS Taxonomy (IAS 7.16(a))",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                    match_strength=MatchStrength.COMPATIBLE,
                    priority=2,
                    semantic_scope="IFRS investing cash flows for PP&E purchases.",
                    verification_source="IASB IFRS Taxonomy (IAS 7.16(a))",
                    legacy_status=False,
                ),
            ],
        ),

        # 13. DILUTED_EPS
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 260-10-45-2)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="DilutedEarningsLossPerShare",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS diluted earnings per share.",
                    verification_source="IASB IFRS Taxonomy (IAS 33.66)",
                    legacy_status=False,
                ),
            ],
        ),

        # 14. DILUTED_WEIGHTED_AVERAGE_SHARES
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
                    verification_source="FASB US-GAAP Taxonomy (ASC 260-10-50-1(a))",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="AdjustedWeightedAverageShares",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS diluted weighted average shares.",
                    verification_source="IASB IFRS Taxonomy (IAS 33.70(b))",
                    legacy_status=False,
                ),
            ],
        ),

        # 15. SHARES_OUTSTANDING
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
                    verification_source="SEC DEI Taxonomy 2020-2026",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="us-gaap",
                    tag="CommonStockSharesOutstanding",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="Balance sheet common shares issued and outstanding.",
                    verification_source="FASB US-GAAP Taxonomy (ASC 210-10-S99-1)",
                    legacy_status=False,
                ),
                ConceptVariant(
                    taxonomy="ifrs-full",
                    tag="NumberOfSharesOutstanding",
                    match_strength=MatchStrength.EXACT,
                    priority=1,
                    semantic_scope="IFRS number of shares outstanding.",
                    verification_source="IASB IFRS Taxonomy (IAS 1.79(a)(iv))",
                    legacy_status=False,
                ),
            ],
        ),
    ]
