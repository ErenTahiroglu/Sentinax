"""
backend/engine/private/portfolio/persistence.py
================================================
Strict Persistence Codec & Serialization/Hydration Boundary for Portfolio Ledger (Phase 12B.2A & 12B.2A.5).

Pure Python — No database I/O, no network calls, no environment variables.

Architectural Invariants:
    - Pure codec between canonical Phase 12A domain dataclasses and database-shaped row dictionaries.
    - Explicit trusted `owner_id` context on all operations (fails closed on mismatch/omission).
    - Strict UUID parsing: Inbound strings must be canonical lowercase hyphenated UUID format (rejects uppercase, braces, whitespace).
    - Strict Date parsing: Inbound strings must be canonical YYYY-MM-DD format (rejects alternative formats, whitespace, datetime objects).
    - Strict exact Decimal parsing: Accepts Decimal or exact decimal str (rejects float, int, bool, NaN, Infinity).
    - Strict Datetime parsing: Timezone-aware datetime or ISO-8601 with tz info only (rejects naive datetimes).
    - Strict Enum parsing: Exact canonical enum values from domain.py; fails closed on missing persisted status/priority columns.
    - Verification of deterministic 64-char SHA-256 `economic_fingerprint` on transaction hydration.
    - Reuses canonical domain model `__post_init__` validation on all hydrations.
    - Strict outbound validation: Serializers validate mutated domain entities before emitting rows (zero float/int/bool coercion).
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

UUID_CANONICAL_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DATE_CANONICAL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ─────────────────────────────────────────────────────────────────────────────
# Inbound Type-Safe Parsing Helpers (Hydration - Fail-Closed)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_required_uuid(val: Any, field_name: str) -> UUID:
    """
    Parses a required UUID.
    Strictly accepts UUID instances or canonical lowercase hyphenated UUID strings.
    Rejects uppercase, braces, hyphenless, whitespace, bool, and int.
    """
    if val is None:
        raise ValueError(f"Required UUID field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, (UUID, str)):
        raise TypeError(f"Field '{field_name}' must be UUID or canonical UUID str, got {type(val).__name__}: {val!r}")
    if isinstance(val, str):
        if not UUID_CANONICAL_PATTERN.match(val):
            raise ValueError(f"Non-canonical or invalid UUID string for '{field_name}': {val!r}")
        try:
            parsed = UUID(val)
            if str(parsed) != val:
                raise ValueError(f"Non-canonical UUID string for '{field_name}': {val!r}")
            return parsed
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
    Strictly accepts date objects or canonical 'YYYY-MM-DD' strings.
    Rejects datetime objects masquerading as date, alternative formats, and whitespace.
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
        if not DATE_CANONICAL_PATTERN.match(val):
            raise ValueError(f"Date string for '{field_name}' must be in canonical YYYY-MM-DD format, got: {val!r}")
        try:
            return date.fromisoformat(val)
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
# Outbound Type-Safe Validation & Serialization Helpers (Fail-Closed)
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_uuid(val: Any, field_name: str) -> str:
    """Validates that a domain field is strictly a UUID instance and formats to canonical str."""
    if val is None:
        raise ValueError(f"Required UUID field '{field_name}' cannot be None.")
    if isinstance(val, bool) or not isinstance(val, UUID):
        raise TypeError(f"Field '{field_name}' must be UUID instance, got {type(val).__name__}: {val!r}")
    return str(val)


def _serialize_optional_uuid(val: Any, field_name: str) -> Optional[str]:
    """Validates an optional UUID field and formats to canonical str."""
    if val is None:
        return None
    return _serialize_uuid(val, field_name)


def _serialize_decimal_positive(val: Any, field_name: str) -> str:
    """
    Validates that a domain field is strictly a finite Decimal > 0.
    Strictly rejects float, int, bool, NaN, and Infinity.
    Formats to fixed-point exact string (never scientific exponent).
    """
    if val is None:
        raise ValueError(f"Required Decimal field '{field_name}' cannot be None.")
    if isinstance(val, bool) or not isinstance(val, Decimal):
        raise TypeError(f"Field '{field_name}' must be Decimal instance, got {type(val).__name__}: {val!r}")
    if not val.is_finite():
        raise ValueError(f"Field '{field_name}' must be finite Decimal, got: {val}")
    if val <= Decimal("0"):
        raise ValueError(f"Field '{field_name}' must be strictly positive (> 0), got: {val}")
    return format(val, "f")


def _serialize_optional_decimal_positive(val: Any, field_name: str) -> Optional[str]:
    """Validates an optional positive Decimal and formats to fixed-point str."""
    if val is None:
        return None
    return _serialize_decimal_positive(val, field_name)


def _serialize_aware_datetime(val: Any, field_name: str) -> str:
    """Validates that a domain field is strictly a timezone-aware datetime instance."""
    if val is None:
        raise ValueError(f"Required datetime field '{field_name}' cannot be None.")
    if isinstance(val, bool) or not isinstance(val, datetime):
        raise TypeError(f"Field '{field_name}' must be datetime instance, got {type(val).__name__}: {val!r}")
    if val.tzinfo is None:
        raise ValueError(f"Datetime field '{field_name}' must be timezone-aware, got naive: {val}")
    return val.isoformat()


def _serialize_optional_aware_datetime(val: Any, field_name: str) -> Optional[str]:
    """Validates an optional timezone-aware datetime."""
    if val is None:
        return None
    return _serialize_aware_datetime(val, field_name)


def _serialize_date(val: Any, field_name: str) -> str:
    """Validates that a domain field is strictly a date instance (not datetime)."""
    if val is None:
        raise ValueError(f"Required date field '{field_name}' cannot be None.")
    if isinstance(val, bool) or isinstance(val, datetime) or not isinstance(val, date):
        raise TypeError(f"Field '{field_name}' must be date instance (not datetime), got {type(val).__name__}: {val!r}")
    return val.isoformat()


def _serialize_optional_date(val: Any, field_name: str) -> Optional[str]:
    """Validates an optional date instance."""
    if val is None:
        return None
    return _serialize_date(val, field_name)


def _serialize_enum(val: Any, enum_cls: Type[E], field_name: str) -> str:
    """Validates that a domain field is strictly an instance of enum_cls."""
    if val is None:
        raise ValueError(f"Required enum field '{field_name}' cannot be None.")
    if not isinstance(val, enum_cls):
        raise TypeError(f"Field '{field_name}' must be {enum_cls.__name__} instance, got {type(val).__name__}: {val!r}")
    return val.value


def _serialize_optional_enum(val: Any, enum_cls: Type[E], field_name: str) -> Optional[str]:
    """Validates an optional enum field."""
    if val is None:
        return None
    return _serialize_enum(val, enum_cls, field_name)


def _serialize_bool(val: Any, field_name: str) -> bool:
    """Validates that a domain field is strictly a bool."""
    if val is None:
        raise ValueError(f"Required bool field '{field_name}' cannot be None.")
    if not isinstance(val, bool):
        raise TypeError(f"Field '{field_name}' must be strict bool, got {type(val).__name__}: {val!r}")
    return val


def _serialize_non_empty_str(val: Any, field_name: str) -> str:
    """Validates that a domain field is strictly a non-empty string."""
    if val is None:
        raise ValueError(f"Required string field '{field_name}' cannot be None.")
    if isinstance(val, bool) or not isinstance(val, str):
        raise TypeError(f"Field '{field_name}' must be str instance, got {type(val).__name__}: {val!r}")
    if not val.strip():
        raise ValueError(f"Field '{field_name}' cannot be empty string.")
    return val


def _serialize_optional_str(val: Any, field_name: str) -> Optional[str]:
    """Validates an optional string field."""
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, str):
        raise TypeError(f"Field '{field_name}' must be str instance, got {type(val).__name__}: {val!r}")
    return val


# ─────────────────────────────────────────────────────────────────────────────
# 1. Portfolio Codec
# ─────────────────────────────────────────────────────────────────────────────

def serialize_portfolio(portfolio: Portfolio, owner_id: UUID | str) -> Dict[str, Any]:
    """
    Serializes a Portfolio domain aggregate into a database row dictionary.
    Whitelists columns from public.portfolios (never includes `is_active`).
    Validates domain entity fields and provenance invariants prior to emission.
    """
    if not isinstance(portfolio, Portfolio):
        raise TypeError(f"Expected Portfolio instance, got {type(portfolio).__name__}")
    portfolio.validate()
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    if portfolio.owner_id is not None:
        domain_owner = _parse_required_uuid(portfolio.owner_id, "portfolio.owner_id")
        if domain_owner != trusted_owner:
            raise ValueError(f"Portfolio.owner_id {domain_owner} does not match trusted owner_id {trusted_owner}")

    return {
        "id": _serialize_uuid(portfolio.id, "portfolio.id"),
        "owner_id": str(trusted_owner),
        "mode": _serialize_enum(portfolio.mode, PortfolioMode, "portfolio.mode"),
        "name": _serialize_non_empty_str(portfolio.name, "portfolio.name"),
        "base_currency": _serialize_enum(portfolio.base_currency, Currency, "portfolio.base_currency"),
        "created_at": _serialize_aware_datetime(portfolio.created_at, "portfolio.created_at"),
        "archived_at": _serialize_optional_aware_datetime(portfolio.archived_at, "portfolio.archived_at"),
        "source_portfolio_id": _serialize_optional_uuid(portfolio.source_portfolio_id, "portfolio.source_portfolio_id"),
        "source_snapshot_time": _serialize_optional_aware_datetime(portfolio.source_snapshot_time, "portfolio.source_snapshot_time"),
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
    Validates domain entity fields prior to emission.
    """
    if not isinstance(account, PortfolioAccount):
        raise TypeError(f"Expected PortfolioAccount instance, got {type(account).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": _serialize_uuid(account.id, "account.id"),
        "portfolio_id": _serialize_uuid(account.portfolio_id, "account.portfolio_id"),
        "owner_id": str(trusted_owner),
        "name": _serialize_non_empty_str(account.name, "account.name"),
        "base_currency": _serialize_enum(account.base_currency, Currency, "account.base_currency"),
        "broker_label": _serialize_optional_str(account.broker_label, "account.broker_label"),
        "created_at": _serialize_aware_datetime(account.created_at, "account.created_at"),
        "archived_at": _serialize_optional_aware_datetime(account.archived_at, "account.archived_at"),
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
    Validates domain entity fields prior to emission.
    """
    if not isinstance(bucket, CashBucket):
        raise TypeError(f"Expected CashBucket instance, got {type(bucket).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": _serialize_uuid(bucket.id, "bucket.id"),
        "portfolio_id": _serialize_uuid(bucket.portfolio_id, "bucket.portfolio_id"),
        "owner_id": str(trusted_owner),
        "account_id": _serialize_optional_uuid(bucket.account_id, "bucket.account_id"),
        "name": _serialize_non_empty_str(bucket.name, "bucket.name"),
        "currency": _serialize_enum(bucket.currency, Currency, "bucket.currency"),
        "purpose": _serialize_enum(bucket.purpose, CashPurpose, "bucket.purpose"),
        "included_in_investable_assets": _serialize_bool(bucket.included_in_investable_assets, "bucket.included_in_investable_assets"),
        "created_at": _serialize_aware_datetime(bucket.created_at, "bucket.created_at"),
        "archived_at": _serialize_optional_aware_datetime(bucket.archived_at, "bucket.archived_at"),
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
    Validates domain entity fields prior to emission.
    """
    if not isinstance(goal, InvestmentGoal):
        raise TypeError(f"Expected InvestmentGoal instance, got {type(goal).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": _serialize_uuid(goal.id, "goal.id"),
        "portfolio_id": _serialize_uuid(goal.portfolio_id, "goal.portfolio_id"),
        "owner_id": str(trusted_owner),
        "name": _serialize_non_empty_str(goal.name, "goal.name"),
        "target_amount": _serialize_decimal_positive(goal.target_amount, "goal.target_amount"),
        "target_currency": _serialize_enum(goal.target_currency, Currency, "goal.target_currency"),
        "target_date": _serialize_optional_date(goal.target_date, "goal.target_date"),
        "priority": _serialize_enum(goal.priority, GoalPriority, "goal.priority"),
        "status": _serialize_enum(goal.status, GoalStatus, "goal.status"),
        "created_at": _serialize_aware_datetime(goal.created_at, "goal.created_at"),
        "archived_at": _serialize_optional_aware_datetime(goal.archived_at, "goal.archived_at"),
    }


def hydrate_investment_goal(row: Dict[str, Any], expected_owner_id: UUID | str) -> InvestmentGoal:
    """
    Hydrates a database row dictionary into a canonical InvestmentGoal domain entity.
    Fails closed if priority or status is missing from the persisted row.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "portfolio_id", "name", "target_amount", "target_currency", "priority", "status", "created_at"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for InvestmentGoal: {missing}")

    priority = _parse_required_enum(row["priority"], GoalPriority, "priority")
    status = _parse_required_enum(row["status"], GoalStatus, "status")

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
    Validates domain entity fields prior to emission.
    """
    if not isinstance(contribution, PlannedContribution):
        raise TypeError(f"Expected PlannedContribution instance, got {type(contribution).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": _serialize_uuid(contribution.id, "contribution.id"),
        "portfolio_id": _serialize_uuid(contribution.portfolio_id, "contribution.portfolio_id"),
        "owner_id": str(trusted_owner),
        "goal_id": _serialize_optional_uuid(contribution.goal_id, "contribution.goal_id"),
        "cash_bucket_id": _serialize_optional_uuid(contribution.cash_bucket_id, "contribution.cash_bucket_id"),
        "expected_date": _serialize_date(contribution.expected_date, "contribution.expected_date"),
        "amount": _serialize_decimal_positive(contribution.amount, "contribution.amount"),
        "currency": _serialize_enum(contribution.currency, Currency, "contribution.currency"),
        "status": _serialize_enum(contribution.status, ContributionStatus, "contribution.status"),
        "created_at": _serialize_aware_datetime(contribution.created_at, "contribution.created_at"),
    }


def hydrate_planned_contribution(row: Dict[str, Any], expected_owner_id: UUID | str) -> PlannedContribution:
    """
    Hydrates a database row dictionary into a canonical PlannedContribution domain entity.
    Fails closed if status is missing from the persisted row.
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected dict row, got {type(row).__name__}")
    _validate_and_get_owner(row, expected_owner_id)

    required_cols = {"id", "portfolio_id", "expected_date", "amount", "currency", "status", "created_at"}
    missing = required_cols - set(row.keys())
    if missing:
        raise KeyError(f"Missing required columns for PlannedContribution: {missing}")

    status = _parse_required_enum(row["status"], ContributionStatus, "status")

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
    Validates domain entity fields prior to emission.
    """
    if not isinstance(transaction, PortfolioTransaction):
        raise TypeError(f"Expected PortfolioTransaction instance, got {type(transaction).__name__}")
    trusted_owner = _parse_required_uuid(owner_id, "owner_id")

    return {
        "id": _serialize_uuid(transaction.id, "transaction.id"),
        "portfolio_id": _serialize_uuid(transaction.portfolio_id, "transaction.portfolio_id"),
        "account_id": _serialize_uuid(transaction.account_id, "transaction.account_id"),
        "owner_id": str(trusted_owner),
        "transaction_type": _serialize_enum(transaction.transaction_type, TransactionType, "transaction.transaction_type"),
        "effective_date": _serialize_date(transaction.effective_date, "transaction.effective_date"),
        "executed_at": _serialize_optional_aware_datetime(transaction.executed_at, "transaction.executed_at"),
        "recorded_at": _serialize_aware_datetime(transaction.recorded_at, "transaction.recorded_at"),
        "instrument_id": _serialize_optional_uuid(transaction.instrument_id, "transaction.instrument_id"),
        "quantity": _serialize_optional_decimal_positive(transaction.quantity, "transaction.quantity"),
        "unit_price": _serialize_optional_decimal_positive(transaction.unit_price, "transaction.unit_price"),
        "trade_currency": _serialize_optional_enum(transaction.trade_currency, Currency, "transaction.trade_currency"),
        "cash_amount": _serialize_optional_decimal_positive(transaction.cash_amount, "transaction.cash_amount"),
        "cash_currency": _serialize_optional_enum(transaction.cash_currency, Currency, "transaction.cash_currency"),
        "cash_bucket_id": _serialize_optional_uuid(transaction.cash_bucket_id, "transaction.cash_bucket_id"),
        "from_currency": _serialize_optional_enum(transaction.from_currency, Currency, "transaction.from_currency"),
        "from_amount": _serialize_optional_decimal_positive(transaction.from_amount, "transaction.from_amount"),
        "to_currency": _serialize_optional_enum(transaction.to_currency, Currency, "transaction.to_currency"),
        "to_amount": _serialize_optional_decimal_positive(transaction.to_amount, "transaction.to_amount"),
        "external_source": _serialize_optional_str(transaction.external_source, "transaction.external_source"),
        "external_reference": _serialize_optional_str(transaction.external_reference, "transaction.external_reference"),
        "reverses_transaction_id": _serialize_optional_uuid(transaction.reverses_transaction_id, "transaction.reverses_transaction_id"),
        "notes": _serialize_optional_str(transaction.notes, "transaction.notes"),
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
