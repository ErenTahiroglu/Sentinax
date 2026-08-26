"""
backend/engine/private/domain.py
==================================
Core domain enumerations and value types for the Private Investment Decision Engine.

These are the canonical types that ALL modules within the Private Engine
must use. No external dependencies — pure Python stdlib only.

Design principles:
    - Point-in-time aware (no look-ahead contamination)
    - Missing data does NOT equal zero
    - PARTIAL results are valid and must propagate correctly
    - No fabrication of financial values
"""

from enum import Enum, auto


# ─────────────────────────────────────────────────────────────────────────────
# Asset Universe
# ─────────────────────────────────────────────────────────────────────────────

class AssetClass(Enum):
    """Top-level asset class for any instrument in the Private Engine universe."""
    EQUITY = "equity"           # Stocks (BIST, US, Europe)
    FUND = "fund"               # Investment funds (TEFAS, UCITS)
    COMMODITY = "commodity"     # Gold, silver
    FX = "fx"                   # Foreign exchange
    FIXED_INCOME = "fixed_income"  # TL bonds/bills, Eurobonds
    ETF = "etf"                 # Exchange-traded funds (US, Europe)


class InstrumentType(Enum):
    """Fine-grained instrument classification within an AssetClass."""
    # Equity
    BIST_STOCK = "bist_stock"
    US_STOCK = "us_stock"
    EUROPEAN_STOCK = "european_stock"

    # Funds
    TEFAS_MONEY_MARKET = "tefas_money_market"
    TEFAS_EQUITY = "tefas_equity"
    TEFAS_VARIABLE = "tefas_variable"
    TEFAS_BALANCED = "tefas_balanced"
    UCITS_FUND = "ucits_fund"

    # ETFs
    US_ETF = "us_etf"
    EUROPEAN_ETF = "european_etf"

    # Commodities
    GOLD = "gold"               # Physical gold / ALTIN.S1
    SILVER = "silver"

    # FX
    USD_TRY = "usd_try"
    EUR_TRY = "eur_try"
    FX_OTHER = "fx_other"

    # Fixed Income
    TL_BOND = "tl_bond"         # Turkish government bond
    TL_TBILL = "tl_tbill"       # Turkish treasury bill
    EUROBOND_TR = "eurobond_tr" # Turkey Eurobond (USD/EUR denominated)

    # Explicitly out of scope — present to allow rejection at ingestion
    CRYPTO = "crypto"           # NOT SUPPORTED — reject at boundary


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio Mode
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioMode(Enum):
    """
    Determines whether the engine is working on a real portfolio or sandbox.
    
    MY_PORTFOLIO: User's actual holdings. Tax, cost, and real-weight aware.
    SANDBOX:      Hypothetical scenario analysis. No real tax/cost constraints.
    """
    MY_PORTFOLIO = "my_portfolio"
    SANDBOX = "sandbox"


# ─────────────────────────────────────────────────────────────────────────────
# Data Quality & Status
# ─────────────────────────────────────────────────────────────────────────────

class DataStatus(Enum):
    """
    Describes the completeness and reliability of a data field or analysis result.

    COMPLETE:     All required inputs present and validated.
    PARTIAL:      Some inputs present; analysis runs but with reduced confidence.
    DEGRADED:     Data present but stale or from a lower-quality source.
    STALE:        Data exists but exceeds acceptable staleness threshold.
    UNAVAILABLE:  No usable data; the field/analysis cannot be computed.

    Rules:
        - UNAVAILABLE does NOT mean zero. It means "we do not know."
        - A PARTIAL analysis is a valid, publishable result.
        - Never fabricate values to avoid UNAVAILABLE status.
    """
    COMPLETE = "complete"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class DataConfidenceLevel(Enum):
    """
    Ordinal confidence in a computed value.
    Used alongside DataStatus to give the consumer a quick signal.
    """
    HIGH = "high"       # Multiple corroborating sources, recent data
    MEDIUM = "medium"   # Single source or slightly stale
    LOW = "low"         # Single source, stale, or proxy value used
    NONE = "none"       # No basis for confidence — value is absent or fabricated guard


class SourceTier(Enum):
    """
    Quality tier of the originating data source.
    Higher tier = more authoritative and point-in-time reliable.
    """
    TIER_1_REGULATORY = "tier_1"    # Regulatory filings (KAP, SEC EDGAR, ESMA)
    TIER_2_EXCHANGE = "tier_2"      # Exchange data (BIST, NYSE, Euronext)
    TIER_3_AGGREGATOR = "tier_3"    # Data aggregators (OpenBB, Bloomberg)
    TIER_4_DERIVED = "tier_4"       # Derived / computed from other sources
    TIER_5_PROXY = "tier_5"         # Best-available proxy — lowest confidence


class ProviderAccessStatus(Enum):
    """
    Production feasibility and operational reliability status of a provider.
    Distinct from SourceTier (authority) and DataConfidenceLevel (data quality).

    GREEN:  Official API / robust production access with stable SLA.
    YELLOW: Scraping-based, free tier, aggressive rate-limit, or degraded SLA.
    RED:    Down, blocked, revoked credentials, or decommissioned.
    """
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class DataCriticality(Enum):
    """
    Contextual importance of a data field within a specific calculation or request.
    Evaluated per-request; never causes global system panic.

    OPTIONAL:  Missing field results in PARTIAL analysis with minor confidence penalty.
    IMPORTANT: Missing field results in DEGRADED analysis with notable confidence penalty.
    CRITICAL:  Missing field renders this specific computation UNAVAILABLE, while allowing
               independent sibling computations to proceed.
    """
    OPTIONAL = "optional"
    IMPORTANT = "important"
    CRITICAL = "critical"


class FreshnessBasis(Enum):
    """
    Specifies which timestamp basis to evaluate for data freshness and staleness.

    EFFECTIVE_DATE: Use the economic effective calendar date (e.g. daily EOD close, fund NAV).
    PUBLISHED_AT:   Use the official release/filing timestamp (e.g. quarterly financial statement, macro CPI).
    OBSERVED_AT:    Use the timestamp when Sentinax first observed/scraped the fact.
    RETRIEVED_AT:   Use the network fetch timestamp.
    """
    EFFECTIVE_DATE = "effective_date"
    PUBLISHED_AT = "published_at"
    OBSERVED_AT = "observed_at"
    RETRIEVED_AT = "retrieved_at"


class AsOfMode(Enum):

    """
    Point-in-Time (PIT) query semantics modes.

    SOURCE_AS_OF:
        Backtest view: returns the fact that was publicly known to the market
        at the specified time: (published_at <= as_of).
        If published_at is NULL, deterministic fallback uses observed_at <= as_of.

    SYSTEM_AS_OF:
        Production / Audit view: returns what Sentinax had actually ingested
        into its database at that moment:
        (published_at IS NULL OR published_at <= as_of) AND ingested_at <= as_of.
    """
    SOURCE_AS_OF = "source_as_of"
    SYSTEM_AS_OF = "system_as_of"


# ─────────────────────────────────────────────────────────────────────────────
# Time / Horizon
# ─────────────────────────────────────────────────────────────────────────────

class Horizon(Enum):

    """
    Investment or analysis horizon.
    Does NOT imply any trading or execution timeframe.
    """
    SHORT = "short"     # 0–3 months
    MEDIUM = "medium"   # 3–18 months
    LONG = "long"       # 18 months+
    UNDEFINED = "undefined"


# ─────────────────────────────────────────────────────────────────────────────
# Currency
# ─────────────────────────────────────────────────────────────────────────────

class Currency(Enum):
    """Supported currencies within the Private Engine universe."""
    TRY = "TRY"   # Turkish Lira
    USD = "USD"   # US Dollar
    EUR = "EUR"   # Euro
    GBP = "GBP"   # British Pound
    XAU = "XAU"   # Gold (troy ounce)
    XAG = "XAG"   # Silver (troy ounce)


# ─────────────────────────────────────────────────────────────────────────────
# Tax
# ─────────────────────────────────────────────────────────────────────────────

class TaxConfidenceClass(Enum):
    """
    How deterministic the tax treatment of a gain/loss is.

    DETERMINISTIC:
        Tax rate and treatment are fully rule-based and computable given
        the instrument type, holding period, and jurisdiction.
        Example: Turkish withholding tax on BIST equity gains.

    USER_INCOME_DEPENDENT:
        Tax impact depends on the user's total annual income or tax bracket.
        Cannot be computed without user's income declaration.
        Example: Income tax on US dividends for Turkish residents.

    PROFESSIONAL_VALIDATION_REQUIRED:
        Complex cross-border treatment, treaty application, or
        entity-level tax considerations that require a licensed advisor.
        Example: Eurobond coupon tax for corporate holders.
    """
    DETERMINISTIC = "deterministic"
    USER_INCOME_DEPENDENT = "user_income_dependent"
    PROFESSIONAL_VALIDATION_REQUIRED = "professional_validation_required"


# ─────────────────────────────────────────────────────────────────────────────
# Instrument Lifecycle & Corporate Actions
# ─────────────────────────────────────────────────────────────────────────────

class InstrumentStatus(Enum):
    """Lifecycle status of a financial instrument."""
    ACTIVE = "active"
    DELISTED = "delisted"
    SUSPENDED = "suspended"
    MERGED = "merged"


class CorporateActionType(Enum):
    """
    Types of corporate actions and reference identity events.
    Used for historical series continuity (conceptually similar to LEAN MapFile/FactorFile).
    """
    SYMBOL_CHANGE = "symbol_change"       # Ticker rename (e.g. FB -> META, or BIST code changes)
    SPLIT = "split"                       # Stock split / reverse split
    DIVIDEND = "dividend"                 # Cash or stock dividend
    MERGER = "merger"                     # Acquisition or merger
    DELISTING = "delisting"               # Removal from exchange listing
    FUND_CODE_CHANGE = "fund_code_change" # TEFAS / UCITS fund code migration

