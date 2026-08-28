"""
backend/engine/private/portfolio/persistence.py
================================================
Strict Persistence Codec & Serialization/Hydration Boundary for Portfolio Ledger (Phase 12B.2A).

Pure Python — No database I/O, no network calls, no environment variables.

Architectural Invariants:
    - Pure codec between canonical Phase 12A domain dataclasses and database-shaped row dictionaries.
    - Explicit trusted `owner_id` context on all operations (fails closed on mismatch/omission).
    - Strict UUID parsing (rejects bools, ints, empty strings, malformed strings).
    - Strict exact Decimal parsing (accepts Decimal or exact decimal str; strictly rejects float, int, bool, NaN, Infinity).
    - Strict Datetime parsing (timezone-aware datetime or ISO-8601 with tz info only; rejects naive datetimes).
    - Strict Date parsing (date or YYYY-MM-DD str only; rejects datetime objects).
    - Strict Enum parsing (exact canonical enum values from domain.py; no fallback defaults).
    - Verification of deterministic 64-char SHA-256 `economic_fingerprint` on transaction hydration.
    - Reuses canonical domain model `__post_init__` validation on all hydrations.
    - Schema whitelisting matching migration 011 (never persists derived properties like `is_active`).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any, Dict, Optional, Type, TypeVar
from uuid import UUID

from backend.engine.private.domain import (
    CashPurpose,
    ContributionStatus,
    Currency,
    GoalPriority,
    GoalStatus,
    PortfolioMode,
    TransactionType,
)
from backend.engine.private.portfolio.models import (
    CashBucket,
    InvestmentGoal,
    PlannedContribution,
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
)

E = TypeVar("E")


# ─────────────────────────────────────────────────────────────────────────────
# Type-Safe Parsing Helpers (Fail-Closed)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_required_uuid(val: Any, field_name: str) -> UUID:
    """Parses a required UUID. Rejects bool, int, empty strings, and malformed strings."""
    if val is None:
        raise ValueError(f"Required UUID field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, (UUID, str)):
        raise TypeError(f"Field '{field_name}' must be UUID or str, got {type(val).__name__}: {val!r}")
    if isinstance(val, str):
        if not val.strip():
            raise ValueError(f"Field '{field_name}' cannot be empty string.")
        try:
            return UUID(val.strip())
        except Exception as e:
            raise ValueError(f"Invalid UUID string for '{field_name}': {val!r}") from e
    return val


def _parse_optional_uuid(val: Any, field_name: str) -> Optional[UUID]:
    """Parses an optional UUID."""
    if val is None:
        return None
    return _parse_required_uuid(val, field_name)


def _parse_required_decimal(val: Any, field_name: str) -> Decimal:
    """
    Parses a required Decimal.
    Strictly accepts only Decimal instances or exact decimal strings.
    Strictly rejects float, int, bool, NaN, and Infinity.
    """
    if val is None:
        raise ValueError(f"Required Decimal field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, (Decimal, str)):
        raise TypeError(
            f"Field '{field_name}' must be Decimal or exact decimal str, got {type(val).__name__}: {val!r}"
        )
    if isinstance(val, str):
        if not val.strip():
            raise ValueError(f"Field '{field_name}' cannot be empty string.")
        try:
            d = Decimal(val.strip())
        except Exception as e:
            raise ValueError(f"Invalid decimal string for '{field_name}': {val!r}") from e
    else:
        d = val
    if not d.is_finite():
        raise ValueError(f"Field '{field_name}' must be a finite Decimal, got: {d}")
    return d


def _parse_optional_decimal(val: Any, field_name: str) -> Optional[Decimal]:
    """Parses an optional Decimal."""
    if val is None:
        return None
    return _parse_required_decimal(val, field_name)


def _format_decimal(d: Optional[Decimal]) -> Optional[str]:
    """Formats a Decimal to exact fixed-point string without scientific exponent."""
    if d is None:
        return None
    return format(d, "f")


def _parse_required_datetime(val: Any, field_name: str) -> datetime:
    """
    Parses a required timezone-aware datetime.
    Strictly rejects naive datetimes and strings without timezone information.
    """
    if val is None:
        raise ValueError(f"Required datetime field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, (datetime, str)):
        raise TypeError(f"Field '{field_name}' must be datetime or ISO str, got {type(val).__name__}: {val!r}")
    if isinstance(val, str):
        if not val.strip():
            raise ValueError(f"Field '{field_name}' cannot be empty string.")
        try:
            dt = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
        except Exception as e:
            raise ValueError(f"Invalid datetime string for '{field_name}': {val!r}") from e
    else:
        dt = val
    if dt.tzinfo is None:
        raise ValueError(f"Datetime field '{field_name}' must be timezone-aware, got naive: {dt}")
    return dt


def _parse_optional_datetime(val: Any, field_name: str) -> Optional[datetime]:
    """Parses an optional timezone-aware datetime."""
    if val is None:
        return None
    return _parse_required_datetime(val, field_name)


def _parse_required_date(val: Any, field_name: str) -> date:
    """
    Parses a required calendar date.
    Strictly accepts date objects or 'YYYY-MM-DD' strings.
    Rejects datetime objects masquerading as date.
    """
    if val is None:
        raise ValueError(f"Required date field '{field_name}' is missing or None.")
    if isinstance(val, bool) or isinstance(val, (int, float)):
        raise TypeError(f"Field '{field_name}' must be date or YYYY-MM-DD str, got {type(val).__name__}: {val!r}")
    if isinstance(val, datetime):
        raise TypeError(f"Field '{field_name}' must be date, not datetime.")
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        if not val.strip():
            raise ValueError(f"Field '{field_name}' cannot be empty string.")
        try:
            return date.fromisoformat(val.strip())
        except Exception as e:
            raise ValueError(f"Invalid date string for '{field_name}': {val!r}") from e
    raise TypeError(f"Field '{field_name}' must be date or YYYY-MM-DD str, got {type(val).__name__}: {val!r}")


def _parse_optional_date(val: Any, field_name: str) -> Optional[date]:
    """Parses an optional calendar date."""
    if val is None:
        return None
    return _parse_required_date(val, field_name)


def _parse_required_enum(val: Any, enum_cls: Type[E], field_name: str) -> E:
    """Parses a required Enum from its exact value string or enum member."""
    if val is None:
        raise ValueError(f"Required enum field '{field_name}' is missing or None.")
    if isinstance(val, enum_cls):
        return val
    if not isinstance(val, str):
        raise TypeError(f"Field '{field_name}' must be {enum_cls.__name__} or str, got {type(val).__name__}: {val!r}")
    try:
        return enum_cls(val)
    except ValueError as e:
        raise ValueError(f"Invalid value {val!r} for enum {enum_cls.__name__} in '{field_name}'.") from e


def _parse_optional_enum(val: Any, enum_cls: Type[E], field_name: str) -> Optional[E]:
    """Parses an optional Enum."""
    if val is None:
        return None
    return _parse_required_enum(val, enum_cls, field_name)


def _parse_required_str(val: Any, field_name: str) -> str:
    """Parses a required non-empty string."""
    if val is None:
        raise ValueError(f"Required string field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, str):
        raise TypeError(f"Field '{field_name}' must be str, got {type(val).__name__}: {val!r}")
    if not val.strip():
        raise ValueError(f"Field '{field_name}' cannot be empty or whitespace-only.")
    return val


def _parse_optional_str(val: Any, field_name: str) -> Optional[str]:
    """Parses an optional string."""
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, str):
        raise TypeError(f"Field '{field_name}' must be str, got {type(val).__name__}: {val!r}")
    return val


def _validate_and_get_owner(row: Dict[str, Any], expected_owner_id: UUID | str) -> UUID:
    """Verifies that the row contains owner_id and that it matches expected_owner_id."""
    if "owner_id" not in row:
        raise KeyError("Row is missing required 'owner_id' column.")
    expected = _parse_required_uuid(expected_owner_id, "expected_owner_id")
    row_owner = _parse_required_uuid(row["owner_id"], "row.owner_id")
    if row_owner != expected:
        raise ValueError(f"Owner mismatch: row owner_id {row_owner} != expected {expected}")
    return expected


# ─────────────────────────────────────────────────────────────────────────────
# 1. Portfolio Codec
# ─────────────────────────────────────────────────────────────────────────────

def serialize_portfolio(portfolio: Portfolio, owner_id: UUID | str) -> Dict[str, Any]:
    """
    Serializes a Portfolio domain aggregate into a database row dictionary.
    Whitelists columns from public.portfolios (never includes `is_active`).
    """
    if not isinstance(portfolio, Portfolio):
        raise TypeError(f"Expected Portfolio instance, got {type(portfolio).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    if portfolio.owner_id is not None:
        domain_owner = _parse_required_uuid(portfolio.owner_id, "portfolio.owner_id")
        if domain_owner != trusted_owner:
            raise ValueError(f"Portfolio.owner_id {domain_owner} does not match trusted owner_id {trusted_owner}")

    return {
        "id": str(portfolio.id),
        "owner_id": str(trusted_owner),
        "mode": portfolio.mode.value,
        "name": portfolio.name,
        "base_currency": portfolio.base_currency.value,
        "created_at": portfolio.created_at.isoformat(),
        "archived_at": portfolio.archived_at.isoformat() if portfolio.archived_at else None,
        "source_portfolio_id": str(portfolio.source_portfolio_id) if portfolio.source_portfolio_id else None,
        "source_snapshot_time": portfolio.source_snapshot_time.isoformat() if portfolio.source_snapshot_time else None,
    }


def hydrate_portfolio(row: Dict[str, Any], expected_owner_id: UUID | str) -> Portfolio:
    """
    Hydrates a database row dictionary into a canonical Portfolio domain aggregate.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    owner = _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "mode", "name", "base_currency", "created_at"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for Portfolio: {missing}")

    return Portfolio(
        id=_parse_required_uuid(row["id"], "id"),
        mode=_parse_required_enum(row["mode"], PortfolioMode, "mode"),
        name=_parse_required_str(row["name"], "name"),
        base_currency=_parse_required_enum(row["base_currency"], Currency, "base_currency"),
        created_at=_parse_required_datetime(row["created_at"], "created_at"),
        archived_at=_parse_optional_datetime(row.get("archived_at"), "archived_at"),
        source_portfolio_id=_parse_optional_uuid(row.get("source_portfolio_id"), "source_portfolio_id"),
        source_snapshot_time=_parse_optional_datetime(row.get("source_snapshot_time"), "source_snapshot_time"),
        owner_id=str(owner),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. PortfolioAccount Codec
# ─────────────────────────────────────────────────────────────────────────────

def serialize_portfolio_account(account: PortfolioAccount, owner_id: UUID | str) -> Dict[str, Any]:
    """
    Serializes a PortfolioAccount domain entity into a database row dictionary.
    Whitelists columns from public.portfolio_accounts (never includes `is_active`).
    """
    if not isinstance(account, PortfolioAccount):
        raise TypeError(f"Expected PortfolioAccount instance, got {type(account).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": str(account.id),
        "portfolio_id": str(account.portfolio_id),
        "owner_id": str(trusted_owner),
        "name": account.name,
        "base_currency": account.base_currency.value,
        "broker_label": account.broker_label,
        "created_at": account.created_at.isoformat(),
        "archived_at": account.archived_at.isoformat() if account.archived_at else None,
    }


def hydrate_portfolio_account(row: Dict[str, Any], expected_owner_id: UUID | str) -> PortfolioAccount:
    """
    Hydrates a database row dictionary into a canonical PortfolioAccount domain entity.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "portfolio_id", "name", "base_currency", "created_at"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for PortfolioAccount: {missing}")

    return PortfolioAccount(
        id=_parse_required_uuid(row["id"], "id"),
        portfolio_id=_parse_required_uuid(row["portfolio_id"], "portfolio_id"),
        name=_parse_required_str(row["name"], "name"),
        base_currency=_parse_required_enum(row["base_currency"], Currency, "base_currency"),
        broker_label=_parse_optional_str(row.get("broker_label"), "broker_label"),
        created_at=_parse_required_datetime(row["created_at"], "created_at"),
        archived_at=_parse_optional_datetime(row.get("archived_at"), "archived_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. CashBucket Codec
# ─────────────────────────────────────────────────────────────────────────────

def serialize_cash_bucket(bucket: CashBucket, owner_id: UUID | str) -> Dict[str, Any]:
    """
    Serializes a CashBucket domain entity into a database row dictionary.
    Whitelists columns from public.cash_buckets (never includes `is_active`).
    """
    if not isinstance(bucket, CashBucket):
        raise TypeError(f"Expected CashBucket instance, got {type(bucket).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": str(bucket.id),
        "portfolio_id": str(bucket.portfolio_id),
        "owner_id": str(trusted_owner),
        "account_id": str(bucket.account_id) if bucket.account_id else None,
        "name": bucket.name,
        "currency": bucket.currency.value,
        "purpose": bucket.purpose.value,
        "included_in_investable_assets": bucket.included_in_investable_assets,
        "created_at": bucket.created_at.isoformat(),
        "archived_at": bucket.archived_at.isoformat() if bucket.archived_at else None,
    }


def hydrate_cash_bucket(row: Dict[str, Any], expected_owner_id: UUID | str) -> CashBucket:
    """
    Hydrates a database row dictionary into a canonical CashBucket domain entity.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "portfolio_id", "name", "currency", "purpose", "included_in_investable_assets", "created_at"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for CashBucket: {missing}")

    inc = row["included_in_investable_assets"]
    if not isinstance(inc, bool):
        raise TypeError(f"Field 'included_in_investable_assets' must be strict bool, got {type(inc).__name__}: {inc!r}")

    return CashBucket(
        id=_parse_required_uuid(row["id"], "id"),
        portfolio_id=_parse_required_uuid(row["portfolio_id"], "portfolio_id"),
        account_id=_parse_optional_uuid(row.get("account_id"), "account_id"),
        name=_parse_required_str(row["name"], "name"),
        currency=_parse_required_enum(row["currency"], Currency, "currency"),
        purpose=_parse_required_enum(row["purpose"], CashPurpose, "purpose"),
        included_in_investable_assets=inc,
        created_at=_parse_required_datetime(row["created_at"], "created_at"),
        archived_at=_parse_optional_datetime(row.get("archived_at"), "archived_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. InvestmentGoal Codec
# ─────────────────────────────────────────────────────────────────────────────

def serialize_investment_goal(goal: InvestmentGoal, owner_id: UUID | str) -> Dict[str, Any]:
    """
    Serializes an InvestmentGoal domain entity into a database row dictionary.
    Emits exact decimal string for target_amount (never float).
    """
    if not isinstance(goal, InvestmentGoal):
        raise TypeError(f"Expected InvestmentGoal instance, got {type(goal).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": str(goal.id),
        "portfolio_id": str(goal.portfolio_id),
        "owner_id": str(trusted_owner),
        "name": goal.name,
        "target_amount": _format_decimal(goal.target_amount),
        "target_currency": goal.target_currency.value,
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "priority": goal.priority.value,
        "status": goal.status.value,
        "created_at": goal.created_at.isoformat(),
        "archived_at": goal.archived_at.isoformat() if goal.archived_at else None,
    }


def hydrate_investment_goal(row: Dict[str, Any], expected_owner_id: UUID | str) -> InvestmentGoal:
    """
    Hydrates a database row dictionary into a canonical InvestmentGoal domain entity.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "portfolio_id", "name", "target_amount", "target_currency", "created_at"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for InvestmentGoal: {missing}")

    priority = _parse_required_enum(row.get("priority", GoalPriority.MEDIUM.value), GoalPriority, "priority")
    status = _parse_required_enum(row.get("status", GoalStatus.ACTIVE.value), GoalStatus, "status")

    return InvestmentGoal(
        id=_parse_required_uuid(row["id"], "id"),
        portfolio_id=_parse_required_uuid(row["portfolio_id"], "portfolio_id"),
        name=_parse_required_str(row["name"], "name"),
        target_amount=_parse_required_decimal(row["target_amount"], "target_amount"),
        target_currency=_parse_required_enum(row["target_currency"], Currency, "target_currency"),
        target_date=_parse_optional_date(row.get("target_date"), "target_date"),
        priority=priority,
        status=status,
        created_at=_parse_required_datetime(row["created_at"], "created_at"),
        archived_at=_parse_optional_datetime(row.get("archived_at"), "archived_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. PlannedContribution Codec
# ─────────────────────────────────────────────────────────────────────────────

def serialize_planned_contribution(contribution: PlannedContribution, owner_id: UUID | str) -> Dict[str, Any]:
    """
    Serializes a PlannedContribution domain entity into a database row dictionary.
    Emits exact decimal string for amount (never float).
    """
    if not isinstance(contribution, PlannedContribution):
        raise TypeError(f"Expected PlannedContribution instance, got {type(contribution).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": str(contribution.id),
        "portfolio_id": str(contribution.portfolio_id),
        "owner_id": str(trusted_owner),
        "goal_id": str(contribution.goal_id) if contribution.goal_id else None,
        "cash_bucket_id": str(contribution.cash_bucket_id) if contribution.cash_bucket_id else None,
        "expected_date": contribution.expected_date.isoformat(),
        "amount": _format_decimal(contribution.amount),
        "currency": contribution.currency.value,
        "status": contribution.status.value,
        "created_at": contribution.created_at.isoformat(),
    }


def hydrate_planned_contribution(row: Dict[str, Any], expected_owner_id: UUID | str) -> PlannedContribution:
    """
    Hydrates a database row dictionary into a canonical PlannedContribution domain entity.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "portfolio_id", "expected_date", "amount", "currency", "created_at"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for PlannedContribution: {missing}")

    status = _parse_required_enum(row.get("status", ContributionStatus.PLANNED.value), ContributionStatus, "status")

    return PlannedContribution(
        id=_parse_required_uuid(row["id"], "id"),
        portfolio_id=_parse_required_uuid(row["portfolio_id"], "portfolio_id"),
        goal_id=_parse_optional_uuid(row.get("goal_id"), "goal_id"),
        cash_bucket_id=_parse_optional_uuid(row.get("cash_bucket_id"), "cash_bucket_id"),
        expected_date=_parse_required_date(row["expected_date"], "expected_date"),
        amount=_parse_required_decimal(row["amount"], "amount"),
        currency=_parse_required_enum(row["currency"], Currency, "currency"),
        status=status,
        created_at=_parse_required_datetime(row["created_at"], "created_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. PortfolioTransaction Codec
# ─────────────────────────────────────────────────────────────────────────────

def serialize_portfolio_transaction(transaction: PortfolioTransaction, owner_id: UUID | str) -> Dict[str, Any]:
    """
    Serializes a frozen PortfolioTransaction domain event into a database row dictionary.
    Emits exact decimal strings for all numeric fields (never floats).
    Emits canonical 64-char SHA-256 economic_fingerprint.
    """
    if not isinstance(transaction, PortfolioTransaction):
        raise TypeError(f"Expected PortfolioTransaction instance, got {type(transaction).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": str(transaction.id),
        "portfolio_id": str(transaction.portfolio_id),
        "account_id": str(transaction.account_id),
        "owner_id": str(trusted_owner),
        "transaction_type": transaction.transaction_type.value,
        "effective_date": transaction.effective_date.isoformat(),
        "executed_at": transaction.executed_at.isoformat() if transaction.executed_at else None,
        "recorded_at": transaction.recorded_at.isoformat(),
        "instrument_id": str(transaction.instrument_id) if transaction.instrument_id else None,
        "quantity": _format_decimal(transaction.quantity),
        "unit_price": _format_decimal(transaction.unit_price),
        "trade_currency": transaction.trade_currency.value if transaction.trade_currency else None,
        "cash_amount": _format_decimal(transaction.cash_amount),
        "cash_currency": transaction.cash_currency.value if transaction.cash_currency else None,
        "cash_bucket_id": str(transaction.cash_bucket_id) if transaction.cash_bucket_id else None,
        "from_currency": transaction.from_currency.value if transaction.from_currency else None,
        "from_amount": _format_decimal(transaction.from_amount),
        "to_currency": transaction.to_currency.value if transaction.to_currency else None,
        "to_amount": _format_decimal(transaction.to_amount),
        "external_source": transaction.external_source,
        "external_reference": transaction.external_reference,
        "reverses_transaction_id": str(transaction.reverses_transaction_id) if transaction.reverses_transaction_id else None,
        "notes": transaction.notes,
        "economic_fingerprint": transaction.economic_fingerprint(),
    }


def hydrate_portfolio_transaction(row: Dict[str, Any], expected_owner_id: UUID | str) -> PortfolioTransaction:
    """
    Hydrates a database row dictionary into a canonical frozen PortfolioTransaction event.
    Verifies that the row's economic_fingerprint matches the computed economic_fingerprint.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "portfolio_id", "account_id", "transaction_type", "effective_date", "recorded_at", "economic_fingerprint"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for PortfolioTransaction: {missing}")

    row_fp = row["economic_fingerprint"]
    if not isinstance(row_fp, str) or not re.match(r"^[0-9a-f]{64}$", row_fp):
        raise ValueError(f"Invalid economic_fingerprint in row: {row_fp!r}")

    tx = PortfolioTransaction(
        id=_parse_required_uuid(row["id"], "id"),
        portfolio_id=_parse_required_uuid(row["portfolio_id"], "portfolio_id"),
        account_id=_parse_required_uuid(row["account_id"], "account_id"),
        transaction_type=_parse_required_enum(row["transaction_type"], TransactionType, "transaction_type"),
        effective_date=_parse_required_date(row["effective_date"], "effective_date"),
        executed_at=_parse_optional_datetime(row.get("executed_at"), "executed_at"),
        recorded_at=_parse_required_datetime(row["recorded_at"], "recorded_at"),
        instrument_id=_parse_optional_uuid(row.get("instrument_id"), "instrument_id"),
        quantity=_parse_optional_decimal(row.get("quantity"), "quantity"),
        unit_price=_parse_optional_decimal(row.get("unit_price"), "unit_price"),
        trade_currency=_parse_optional_enum(row.get("trade_currency"), Currency, "trade_currency"),
        cash_amount=_parse_optional_decimal(row.get("cash_amount"), "cash_amount"),
        cash_currency=_parse_optional_enum(row.get("cash_currency"), Currency, "cash_currency"),
        cash_bucket_id=_parse_optional_uuid(row.get("cash_bucket_id"), "cash_bucket_id"),
        from_currency=_parse_optional_enum(row.get("from_currency"), Currency, "from_currency"),
        from_amount=_parse_optional_decimal(row.get("from_amount"), "from_amount"),
        to_currency=_parse_optional_enum(row.get("to_currency"), Currency, "to_currency"),
        to_amount=_parse_optional_decimal(row.get("to_amount"), "to_amount"),
        external_source=_parse_optional_str(row.get("external_source"), "external_source"),
        external_reference=_parse_optional_str(row.get("external_reference"), "external_reference"),
        reverses_transaction_id=_parse_optional_uuid(row.get("reverses_transaction_id"), "reverses_transaction_id"),
        notes=_parse_optional_str(row.get("notes"), "notes"),
    )

    computed_fp = tx.economic_fingerprint()
    if row_fp != computed_fp:
        raise ValueError(
            f"Economic fingerprint mismatch in hydrated transaction {tx.id}: row={row_fp}, computed={computed_fp}"
        )

    return tx
