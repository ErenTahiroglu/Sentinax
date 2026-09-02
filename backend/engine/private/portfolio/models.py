"""
backend/engine/private/portfolio/models.py
===========================================
Immutable Domain Models for the Private Personal Investment Decision Engine.

Key Architectural Invariants:
    - Transactions are the SINGLE AUTHORITATIVE SOURCE OF TRUTH.
    - Positions, lots, cash balances, and portfolio values are DERIVED PROJECTIONS.
    - Two independent time axes:
        1. Economic event time: `effective_date` / `executed_at`
        2. System knowledge time: `recorded_at`
    - Strict Decimal typing (no float, no NaN, no Inf, no silent float conversions).
    - Hard bounded contexts between MY_PORTFOLIO and SANDBOX.
    - Zero user secrets, credentials, or unnecessary PII.
    - Mutually exclusive event field families (fail-closed on contradictory fields).
    - REVERSAL is strictly reference-only (zero independent economic fields).
    - External source/reference pair is all-or-none with non-empty string validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from backend.engine.private.domain import (
    CashPurpose,
    ContributionStatus,
    Currency,
    GoalPriority,
    GoalStatus,
    LotStatus,
    PortfolioMode,
    TransactionType,
)
from backend.engine.private.portfolio.normalization import (
    normalize_external_reference,
    normalize_external_source,
)


def _validate_decimal_positive(val: Any, field_name: str) -> Decimal:
    """Validates that a value is strictly a finite Decimal > 0 (not float, not bool, not int/str)."""
    if isinstance(val, bool) or not isinstance(val, Decimal):
        raise TypeError(f"{field_name} must be a Decimal, got {type(val).__name__}: {val!r}")
    if not val.is_finite():
        raise ValueError(f"{field_name} must be finite, got: {val}")
    if val <= Decimal("0"):
        raise ValueError(f"{field_name} must be strictly positive (> 0), got: {val}")
    return val


def _validate_decimal_nonnegative(val: Any, field_name: str) -> Decimal:
    """Validates that a value is strictly a finite Decimal >= 0 (not float, not bool, not int/str)."""
    if isinstance(val, bool) or not isinstance(val, Decimal):
        raise TypeError(f"{field_name} must be a Decimal, got {type(val).__name__}: {val!r}")
    if not val.is_finite():
        raise ValueError(f"{field_name} must be finite, got: {val}")
    if val < Decimal("0"):
        raise ValueError(f"{field_name} must be non-negative (>= 0), got: {val}")
    return val


def _validate_aware_datetime(dt: Optional[datetime], field_name: str) -> None:
    """Validates that a datetime is timezone-aware."""
    if dt is not None and dt.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got naive: {dt}")


def _validate_strict_calendar_date(d: Optional[date], field_name: str, required: bool = True) -> Optional[date]:
    """
    Validates that a value is strictly a Python date object (and not datetime, bool, str, int, etc.).
    Preserves the date exactly without temporal-policy enforcement (historical/overdue dates valid).
    """
    if d is None:
        if required:
            raise ValueError(f"{field_name} is required and cannot be None.")
        return None
    if isinstance(d, datetime):
        raise TypeError(f"{field_name} must be a strict date, not datetime: {d!r}")
    if isinstance(d, bool) or not isinstance(d, date):
        raise TypeError(f"{field_name} must be a date, got {type(d).__name__}: {d!r}")
    return d


def _canonical_decimal_str(d: Optional[Decimal]) -> Optional[str]:
    """
    Renders a finite Decimal in canonical text form for economic fingerprinting.
    Numerically equivalent Decimals (e.g. 1, 1.0, 1.00, 1E+0, 1E+3, 1000.00) produce identical text.
    - None -> None (serialized to JSON null).
    - No float conversion.
    - No context-dependent rounding.
    - No precision loss on arbitrarily large/small finite Decimals.
    """
    if d is None:
        return None
    if not isinstance(d, Decimal) or isinstance(d, bool):
        raise TypeError(f"Expected Decimal, got {type(d).__name__}: {d!r}")
    if not d.is_finite():
        raise ValueError(f"Expected finite Decimal, got: {d}")

    # Format to fixed-point string
    s = format(d, "f")
    if "." in s:
        # Strip trailing fractional zeros and trailing dot
        s = s.rstrip("0").rstrip(".")
    if s == "-0":
        s = "0"
    return s


def _canonical_datetime_str(dt: Optional[datetime]) -> Optional[str]:
    """
    Renders an aware datetime in canonical UTC instant text form for economic fingerprinting.
    Numerically/chronologically equivalent instants with different timezone representations
    (e.g. 2026-08-28T10:00:00+00:00 and 2026-08-28T13:00:00+03:00) produce identical text.
    - None -> None (serialized to JSON null).
    - Requires actual datetime
    - Requires timezone-aware (tzinfo is not None)
    - Converts to UTC with dt.astimezone(timezone.utc)
    - Formats with isoformat()
    - Does not drop microseconds
    """
    if dt is None:
        return None
    if isinstance(dt, bool) or not isinstance(dt, datetime):
        raise TypeError(f"Expected datetime instance, got {type(dt).__name__}: {dt!r}")
    if dt.tzinfo is None:
        raise ValueError(f"Datetime must be timezone-aware, got naive: {dt}")
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Portfolio Model (Lifecycle Entity)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Portfolio:
    """
    Root portfolio domain aggregate representing either real user holdings or an isolated sandbox.
    """
    mode: PortfolioMode
    name: str
    base_currency: Currency
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
    archived_at: Optional[datetime] = None
    source_portfolio_id: Optional[UUID] = None
    source_snapshot_time: Optional[datetime] = None
    owner_id: Optional[str] = None

    def validate(self) -> None:
        """Validates all Portfolio domain invariants."""
        if not self.name or not self.name.strip():
            raise ValueError("Portfolio name cannot be empty.")
        if self.created_at.tzinfo is None:
            raise ValueError(f"created_at must be timezone-aware, got naive: {self.created_at}")
        _validate_aware_datetime(self.archived_at, "archived_at")
        _validate_aware_datetime(self.source_snapshot_time, "source_snapshot_time")

        if self.mode == PortfolioMode.MY_PORTFOLIO:
            if self.source_portfolio_id is not None:
                raise ValueError("MY_PORTFOLIO cannot have source_portfolio_id (cloning/provenance is SANDBOX only).")
            if self.source_snapshot_time is not None:
                raise ValueError("MY_PORTFOLIO cannot have source_snapshot_time.")

        if self.mode == PortfolioMode.SANDBOX:
            if self.source_snapshot_time is not None and self.source_portfolio_id is None:
                raise ValueError("SANDBOX with source_snapshot_time must specify source_portfolio_id.")
            if self.source_portfolio_id is not None and self.source_portfolio_id == self.id:
                raise ValueError("SANDBOX source_portfolio_id cannot reference self (no self-cloning).")

    def __post_init__(self) -> None:
        self.validate()

    @property
    def is_active(self) -> bool:
        return self.archived_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "mode": self.mode.value,
            "name": self.name,
            "base_currency": self.base_currency.value,
            "created_at": self.created_at.isoformat(),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "source_portfolio_id": str(self.source_portfolio_id) if self.source_portfolio_id else None,
            "source_snapshot_time": self.source_snapshot_time.isoformat() if self.source_snapshot_time else None,
            "owner_id": self.owner_id,
            "is_active": self.is_active,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Portfolio Account Model (Lifecycle Entity)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PortfolioAccount:
    """
    Custody, brokerage, or manual ledger account within a Portfolio.
    Allows same canonical instrument to exist across multiple accounts with separate tax/lot tracking.
    """
    portfolio_id: UUID
    name: str
    base_currency: Currency
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
    broker_label: Optional[str] = None
    archived_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Account name cannot be empty.")
        if self.created_at.tzinfo is None:
            raise ValueError(f"created_at must be timezone-aware, got naive: {self.created_at}")
        _validate_aware_datetime(self.archived_at, "archived_at")

    @property
    def is_active(self) -> bool:
        return self.archived_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "portfolio_id": str(self.portfolio_id),
            "name": self.name,
            "base_currency": self.base_currency.value,
            "broker_label": self.broker_label,
            "created_at": self.created_at.isoformat(),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "is_active": self.is_active,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Immutable Transaction Event Model (Frozen Ledger Event)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PortfolioTransaction:
    """
    Immutable economic event recorded in the portfolio ledger.

    Invariants:
        - No mutation methods / frozen dataclass.
        - Two independent time axes:
            * `effective_date` / `executed_at`: Economic event time.
            * `recorded_at`: System knowledge time (when Sentinax ingested it).
        - Strict Decimal type checks on all financial amounts.
        - Exact multi-currency preservation (no implicit conversions).
        - Mutually exclusive field families per transaction type.
        - REVERSAL is strictly reference-only (zero independent economic fields).
        - External source/reference pair is all-or-none with non-empty string validation.
    """
    portfolio_id: UUID
    account_id: UUID
    transaction_type: TransactionType
    effective_date: date
    recorded_at: datetime

    id: UUID = field(default_factory=uuid4)
    executed_at: Optional[datetime] = None

    # Trade security fields (BUY, SELL)
    instrument_id: Optional[UUID] = None
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    trade_currency: Optional[Currency] = None

    # Cash movement fields (CASH_DEPOSIT, CASH_WITHDRAWAL, DIVIDEND, INTEREST, FEE, TAX_WITHHOLDING)
    cash_amount: Optional[Decimal] = None
    cash_currency: Optional[Currency] = None
    cash_bucket_id: Optional[UUID] = None

    # FX Conversion fields (two-leg economics)
    from_currency: Optional[Currency] = None
    from_amount: Optional[Decimal] = None
    to_currency: Optional[Currency] = None
    to_amount: Optional[Decimal] = None

    # External source & idempotency (all-or-none pair)
    external_source: Optional[str] = None
    external_reference: Optional[str] = None

    # Reversal / correction link
    reverses_transaction_id: Optional[UUID] = None

    # Context notes (metadata only, excluded from economic fingerprint)
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        # 1. Timezone checks
        if self.recorded_at.tzinfo is None:
            raise ValueError(f"recorded_at must be timezone-aware, got naive: {self.recorded_at}")
        if self.executed_at is not None and self.executed_at.tzinfo is None:
            raise ValueError(f"executed_at must be timezone-aware, got naive: {self.executed_at}")

        # 2. External source & reference pairing validation (Phase 12A.6)
        if self.external_source is None and self.external_reference is None:
            pass  # Valid manual / internal event
        elif self.external_source is not None and self.external_reference is not None:
            if not isinstance(self.external_source, str) or isinstance(self.external_source, bool):
                raise TypeError(
                    f"external_source must be a str, got {type(self.external_source).__name__}: {self.external_source!r}"
                )
            if not isinstance(self.external_reference, str) or isinstance(self.external_reference, bool):
                raise TypeError(
                    f"external_reference must be a str, got {type(self.external_reference).__name__}: {self.external_reference!r}"
                )
            norm_source = normalize_external_source(self.external_source)
            norm_ref = normalize_external_reference(self.external_reference)
            if not norm_source:
                raise ValueError("external_source cannot be empty or whitespace-only.")
            if not norm_ref:
                raise ValueError("external_reference cannot be empty or whitespace-only.")
        else:
            if self.external_source is not None and self.external_reference is None:
                raise ValueError("external_source is provided but external_reference is missing.")
            else:
                raise ValueError("external_reference is provided but external_source is missing.")

        # 3. Per-TransactionType Contract Validation
        t = self.transaction_type

        if t in (TransactionType.BUY, TransactionType.SELL):
            if self.instrument_id is None:
                raise ValueError(f"{t.name} requires instrument_id.")
            if self.quantity is None:
                raise ValueError(f"{t.name} requires quantity.")
            _validate_decimal_positive(self.quantity, f"{t.name} quantity")
            if self.unit_price is None:
                raise ValueError(f"{t.name} requires unit_price.")
            _validate_decimal_positive(self.unit_price, f"{t.name} unit_price")
            if self.trade_currency is None:
                raise ValueError(f"{t.name} requires trade_currency.")

            # Contradictory economics check: No cash fields, no FX fields, no reversal field
            if self.cash_amount is not None or self.cash_currency is not None:
                raise ValueError(f"{t.name} must not contain cash_amount or cash_currency.")
            if self.from_currency is not None or self.from_amount is not None or self.to_currency is not None or self.to_amount is not None:
                raise ValueError(f"{t.name} must not contain FX conversion legs.")
            if self.reverses_transaction_id is not None:
                raise ValueError(f"{t.name} must not have reverses_transaction_id (use REVERSAL type).")

        elif t in (TransactionType.CASH_DEPOSIT, TransactionType.CASH_WITHDRAWAL):
            if self.cash_amount is None:
                raise ValueError(f"{t.name} requires cash_amount.")
            _validate_decimal_positive(self.cash_amount, f"{t.name} cash_amount")
            if self.cash_currency is None:
                raise ValueError(f"{t.name} requires cash_currency.")

            # Contradictory economics check: No security fields (including instrument_id), no FX fields, no reversal field
            if self.instrument_id is not None:
                raise ValueError(f"{t.name} must not contain instrument_id.")
            if self.quantity is not None or self.unit_price is not None or self.trade_currency is not None:
                raise ValueError(f"{t.name} must not contain trade security fields.")
            if self.from_currency is not None or self.from_amount is not None or self.to_currency is not None or self.to_amount is not None:
                raise ValueError(f"{t.name} must not contain FX conversion legs.")
            if self.reverses_transaction_id is not None:
                raise ValueError(f"{t.name} must not have reverses_transaction_id (use REVERSAL type).")

        elif t in (TransactionType.DIVIDEND, TransactionType.INTEREST, TransactionType.FEE, TransactionType.TAX_WITHHOLDING):
            if self.cash_amount is None:
                raise ValueError(f"{t.name} requires cash_amount.")
            _validate_decimal_positive(self.cash_amount, f"{t.name} cash_amount")
            if self.cash_currency is None:
                raise ValueError(f"{t.name} requires cash_currency.")

            # Contradictory economics check: No trade pricing/quantity fields, no FX fields, no reversal field
            if self.quantity is not None or self.unit_price is not None or self.trade_currency is not None:
                raise ValueError(f"{t.name} must not contain quantity, unit_price, or trade_currency.")
            if self.from_currency is not None or self.from_amount is not None or self.to_currency is not None or self.to_amount is not None:
                raise ValueError(f"{t.name} must not contain FX conversion legs.")
            if self.reverses_transaction_id is not None:
                raise ValueError(f"{t.name} must not have reverses_transaction_id (use REVERSAL type).")

        elif t == TransactionType.FX_CONVERSION:
            if self.from_currency is None:
                raise ValueError("FX_CONVERSION requires from_currency.")
            if self.from_amount is None:
                raise ValueError("FX_CONVERSION requires from_amount.")
            _validate_decimal_positive(self.from_amount, "from_amount")

            if self.to_currency is None:
                raise ValueError("FX_CONVERSION requires to_currency.")
            if self.to_amount is None:
                raise ValueError("FX_CONVERSION requires to_amount.")
            _validate_decimal_positive(self.to_amount, "to_amount")

            if self.from_currency == self.to_currency:
                raise ValueError(f"FX_CONVERSION requires distinct currencies, got {self.from_currency} on both legs.")

            # Contradictory economics check: No security fields, no simple cash fields, no cash_bucket_id, no reversal field
            if self.instrument_id is not None:
                raise ValueError("FX_CONVERSION must not contain instrument_id.")
            if self.quantity is not None or self.unit_price is not None or self.trade_currency is not None:
                raise ValueError("FX_CONVERSION must not contain security trade fields.")
            if self.cash_amount is not None or self.cash_currency is not None:
                raise ValueError("FX_CONVERSION must not contain cash_amount or cash_currency.")
            if self.cash_bucket_id is not None:
                raise ValueError("FX_CONVERSION must not contain cash_bucket_id.")
            if self.reverses_transaction_id is not None:
                raise ValueError("FX_CONVERSION must not have reverses_transaction_id (use REVERSAL type).")

        elif t == TransactionType.REVERSAL:
            if self.reverses_transaction_id is None:
                raise ValueError("REVERSAL requires reverses_transaction_id.")
            if self.reverses_transaction_id == self.id:
                raise ValueError("Transaction cannot reverse itself (self-reversal).")

            # REVERSAL is strictly reference-only: All independent economic fields MUST be None
            if self.instrument_id is not None:
                raise ValueError("REVERSAL must not contain instrument_id.")
            if self.quantity is not None or self.unit_price is not None or self.trade_currency is not None:
                raise ValueError("REVERSAL must not contain quantity, unit_price, or trade_currency.")
            if self.cash_amount is not None or self.cash_currency is not None:
                raise ValueError("REVERSAL must not contain cash_amount or cash_currency.")
            if self.cash_bucket_id is not None:
                raise ValueError("REVERSAL must not contain cash_bucket_id.")
            if self.from_currency is not None or self.from_amount is not None or self.to_currency is not None or self.to_amount is not None:
                raise ValueError("REVERSAL must not contain FX conversion fields.")

    def economic_fingerprint(self) -> str:
        """
        Computes deterministic SHA-256 economic fingerprint over an unambiguous structured payload.
        Excludes physical internal `id`, `recorded_at`, and mutable `notes`.
        """
        ext_source_str = normalize_external_source(self.external_source)
        ext_ref_str = normalize_external_reference(self.external_reference)

        payload = [
            str(self.portfolio_id),
            str(self.account_id),
            self.transaction_type.value,
            str(self.instrument_id) if self.instrument_id else None,
            self.effective_date.isoformat(),
            _canonical_datetime_str(self.executed_at),
            _canonical_decimal_str(self.quantity),
            _canonical_decimal_str(self.unit_price),
            self.trade_currency.value if self.trade_currency else None,
            _canonical_decimal_str(self.cash_amount),
            self.cash_currency.value if self.cash_currency else None,
            str(self.cash_bucket_id) if self.cash_bucket_id else None,
            self.from_currency.value if self.from_currency else None,
            _canonical_decimal_str(self.from_amount),
            self.to_currency.value if self.to_currency else None,
            _canonical_decimal_str(self.to_amount),
            ext_source_str,
            ext_ref_str,
            str(self.reverses_transaction_id) if self.reverses_transaction_id else None,
        ]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "portfolio_id": str(self.portfolio_id),
            "account_id": str(self.account_id),
            "transaction_type": self.transaction_type.value,
            "effective_date": self.effective_date.isoformat(),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "recorded_at": self.recorded_at.isoformat(),
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
            "trade_currency": self.trade_currency.value if self.trade_currency else None,
            "cash_amount": str(self.cash_amount) if self.cash_amount is not None else None,
            "cash_currency": self.cash_currency.value if self.cash_currency else None,
            "cash_bucket_id": str(self.cash_bucket_id) if self.cash_bucket_id else None,
            "from_currency": self.from_currency.value if self.from_currency else None,
            "from_amount": str(self.from_amount) if self.from_amount is not None else None,
            "to_currency": self.to_currency.value if self.to_currency else None,
            "to_amount": str(self.to_amount) if self.to_amount is not None else None,
            "external_source": self.external_source,
            "external_reference": self.external_reference,
            "reverses_transaction_id": str(self.reverses_transaction_id) if self.reverses_transaction_id else None,
            "notes": self.notes,
            "economic_fingerprint": self.economic_fingerprint(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cash Bucket Model (Lifecycle Entity)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CashBucket:
    """
    Categorizes personal cash into liquidity/purpose buckets (e.g. Investable vs Emergency Reserve).
    Protects emergency/near-term money from being treated as investable asset base.
    """
    portfolio_id: UUID
    name: str
    currency: Currency
    purpose: CashPurpose
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
    account_id: Optional[UUID] = None
    included_in_investable_assets: Optional[bool] = None
    archived_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Cash bucket name cannot be empty.")
        if self.created_at.tzinfo is None:
            raise ValueError(f"created_at must be timezone-aware, got naive: {self.created_at}")
        _validate_aware_datetime(self.archived_at, "archived_at")

        # Purpose-driven default inclusion or strict bool check
        if self.included_in_investable_assets is None:
            if self.purpose == CashPurpose.INVESTABLE:
                self.included_in_investable_assets = True
            else:
                self.included_in_investable_assets = False
        else:
            if not isinstance(self.included_in_investable_assets, bool):
                raise TypeError(
                    f"included_in_investable_assets must be a strict bool (or None), "
                    f"got {type(self.included_in_investable_assets).__name__}: {self.included_in_investable_assets!r}"
                )

    @property
    def is_active(self) -> bool:
        return self.archived_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "portfolio_id": str(self.portfolio_id),
            "account_id": str(self.account_id) if self.account_id else None,
            "name": self.name,
            "currency": self.currency.value,
            "purpose": self.purpose.value,
            "included_in_investable_assets": self.included_in_investable_assets,
            "created_at": self.created_at.isoformat(),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "is_active": self.is_active,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Investment Goal Model (Lifecycle Entity)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InvestmentGoal:
    """
    User investment/financial goal with explicit target amount, currency, and arbitrary target date.
    """
    portfolio_id: UUID
    name: str
    target_amount: Decimal
    target_currency: Currency
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
    target_date: Optional[date] = None
    priority: GoalPriority = GoalPriority.MEDIUM
    status: GoalStatus = GoalStatus.ACTIVE
    archived_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Goal name cannot be empty.")
        _validate_decimal_positive(self.target_amount, "target_amount")
        _validate_strict_calendar_date(self.target_date, "target_date", required=False)
        if self.created_at.tzinfo is None:
            raise ValueError(f"created_at must be timezone-aware, got naive: {self.created_at}")
        _validate_aware_datetime(self.archived_at, "archived_at")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "portfolio_id": str(self.portfolio_id),
            "name": self.name,
            "target_amount": str(self.target_amount),
            "target_currency": self.target_currency.value,
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "priority": self.priority.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 6. Planned Contribution Model (Lifecycle Entity)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlannedContribution:
    """
    Expected future inflow tied to a goal or cash bucket.
    CRITICAL: A planned contribution is NOT portfolio cash. It does not alter the ledger.
    """
    portfolio_id: UUID
    expected_date: date
    amount: Decimal
    currency: Currency
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
    goal_id: Optional[UUID] = None
    cash_bucket_id: Optional[UUID] = None
    status: ContributionStatus = ContributionStatus.PLANNED

    def __post_init__(self) -> None:
        _validate_strict_calendar_date(self.expected_date, "expected_date", required=True)
        _validate_decimal_positive(self.amount, "PlannedContribution amount")
        if self.created_at.tzinfo is None:
            raise ValueError(f"created_at must be timezone-aware, got naive: {self.created_at}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "portfolio_id": str(self.portfolio_id),
            "goal_id": str(self.goal_id) if self.goal_id else None,
            "cash_bucket_id": str(self.cash_bucket_id) if self.cash_bucket_id else None,
            "expected_date": self.expected_date.isoformat(),
            "amount": str(self.amount),
            "currency": self.currency.value,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 7. Position / Tax Lot Model (Projection Boundary Only)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionLot:
    """
    Derived accounting / tax lot produced by ledger projections.
    NEVER the primary source of truth; strictly derived from immutable transactions.
    """
    portfolio_id: UUID
    account_id: UUID
    instrument_id: UUID
    origin_transaction_id: UUID
    acquired_date: date
    quantity_open: Decimal
    original_quantity: Decimal
    native_unit_cost: Decimal
    currency: Currency
    id: UUID = field(default_factory=uuid4)
    status: LotStatus = LotStatus.OPEN

    def __post_init__(self) -> None:
        _validate_decimal_positive(self.original_quantity, "original_quantity")
        _validate_decimal_nonnegative(self.quantity_open, "quantity_open")
        if self.quantity_open > self.original_quantity:
            raise ValueError(
                f"quantity_open ({self.quantity_open}) cannot exceed original_quantity ({self.original_quantity})."
            )
        _validate_decimal_nonnegative(self.native_unit_cost, "native_unit_cost")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "portfolio_id": str(self.portfolio_id),
            "account_id": str(self.account_id),
            "instrument_id": str(self.instrument_id),
            "origin_transaction_id": str(self.origin_transaction_id),
            "acquired_date": self.acquired_date.isoformat(),
            "quantity_open": str(self.quantity_open),
            "original_quantity": str(self.original_quantity),
            "native_unit_cost": str(self.native_unit_cost),
            "currency": self.currency.value,
            "status": self.status.value,
        }
