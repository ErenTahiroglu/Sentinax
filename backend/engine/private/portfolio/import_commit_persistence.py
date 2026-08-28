"""
Strict Persistence Codec & Serialization/Hydration Boundary for Import Claim-to-Ledger Bindings (Phase 13P).

Pure Python — Zero database I/O, zero network calls, zero environment variables, zero UUID generation,
zero system clock calls, and zero new hash calculations.

Key Architectural Guarantees:
1. Strict Pure Codec: Pure boundary mapping between Phase 13O ImportLedgerBindingIntent and
   PostgreSQL/Supabase row dictionaries for public.portfolio_import_claim_bindings.
2. Exact Raw Claim Identity Uniqueness: Primary key is strictly the composite raw claim identity:
   (portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256).
3. Interpretation Separation: expected_plan_sha256 is explicitly stored as the interpretation snapshot
   but excluded from the claim identity primary key.
4. Target Foreign Key Binding: transaction_id binds the raw claim to a ledger event; many claims to one
   transaction is structurally supported.
5. Defense-in-Depth Owner Isolation: Trusted expected_owner_id context enforced on all operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Dict, Optional, Tuple, Union
from uuid import UUID

from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingIntent,
)

UUID_CANONICAL_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SOURCE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PortfolioImportCommitPersistenceError(ValueError):
    """Raised when import claim persistence serialization or hydration validation fails closed."""
    pass


def _parse_strict_uuid(val: Any, field_name: str) -> UUID:
    """
    Parses and strictly validates a UUID field.
    Accepts UUID instances or canonical lowercase hyphenated UUID strings.
    Rejects uppercase, braces, hyphenless, whitespace, bool, and int.
    """
    if val is None:
        raise PortfolioImportCommitPersistenceError(f"Required UUID field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, (UUID, str)):
        raise PortfolioImportCommitPersistenceError(
            f"Field '{field_name}' must be UUID or canonical UUID str, got {type(val).__name__}: {val!r}"
        )
    if isinstance(val, str):
        if not UUID_CANONICAL_PATTERN.fullmatch(val):
            raise PortfolioImportCommitPersistenceError(f"Non-canonical or invalid UUID string for '{field_name}': {val!r}")
        try:
            parsed = UUID(val)
            if str(parsed) != val:
                raise PortfolioImportCommitPersistenceError(f"Non-canonical UUID string for '{field_name}': {val!r}")
            return parsed
        except Exception as e:
            raise PortfolioImportCommitPersistenceError(f"Invalid UUID string for '{field_name}': {val!r}") from e
    return val


def _validate_strict_source_key(val: Any, field_name: str = "source_key") -> str:
    """Strictly validates source_key grammar without normalization."""
    if val is None:
        raise PortfolioImportCommitPersistenceError(f"Required field '{field_name}' is missing or None.")
    if isinstance(val, bool) or type(val) is not str:
        raise PortfolioImportCommitPersistenceError(
            f"Field '{field_name}' must be a str instance, got {type(val).__name__}: {val!r}"
        )
    if not SOURCE_KEY_PATTERN.fullmatch(val):
        raise PortfolioImportCommitPersistenceError(
            f"Invalid source_key format for '{field_name}': {val!r}"
        )
    return val


def _validate_strict_sha256_hex(val: Any, field_name: str) -> str:
    """Strictly validates 64-character lowercase hexadecimal SHA-256 string without normalization."""
    if val is None:
        raise PortfolioImportCommitPersistenceError(f"Required field '{field_name}' is missing or None.")
    if isinstance(val, bool) or type(val) is not str:
        raise PortfolioImportCommitPersistenceError(
            f"Field '{field_name}' must be a str instance, got {type(val).__name__}: {val!r}"
        )
    if not SHA256_HEX_PATTERN.fullmatch(val):
        raise PortfolioImportCommitPersistenceError(
            f"Field '{field_name}' must be a 64-character lowercase hex string, got {val!r}"
        )
    return val


def _parse_strict_record_ordinal(val: Any, field_name: str = "record_ordinal") -> int:
    """Strictly validates positive integer record_ordinal."""
    if val is None:
        raise PortfolioImportCommitPersistenceError(f"Required field '{field_name}' is missing or None.")
    if isinstance(val, bool) or type(val) is not int:
        raise PortfolioImportCommitPersistenceError(
            f"Field '{field_name}' must be an int instance, got {type(val).__name__}: {val!r}"
        )
    if val < 1:
        raise PortfolioImportCommitPersistenceError(
            f"Field '{field_name}' must be a positive integer (>= 1), got {val}"
        )
    return val


def _parse_strict_datetime(val: Any, field_name: str = "bound_at") -> datetime:
    """Strictly validates timezone-aware datetime."""
    if val is None:
        raise PortfolioImportCommitPersistenceError(f"Required datetime field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, (datetime, str)):
        raise PortfolioImportCommitPersistenceError(
            f"Field '{field_name}' must be datetime or ISO str, got {type(val).__name__}: {val!r}"
        )
    if isinstance(val, str):
        if not val.strip():
            raise PortfolioImportCommitPersistenceError(f"Field '{field_name}' cannot be empty string.")
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception as e:
            raise PortfolioImportCommitPersistenceError(f"Invalid datetime string for '{field_name}': {val!r}") from e
    else:
        dt = val
    if dt.tzinfo is None:
        raise PortfolioImportCommitPersistenceError(
            f"Datetime field '{field_name}' must be timezone-aware, got naive: {dt}"
        )
    return dt


@dataclass(frozen=True)
class PersistedImportLedgerBinding:
    """
    Immutable representation of a persisted claim-to-transaction binding row
    from public.portfolio_import_claim_bindings.
    """
    owner_id: UUID
    portfolio_id: UUID
    account_id: UUID

    source_key: str
    file_content_sha256: str
    record_ordinal: int
    record_sha256: str

    expected_plan_sha256: str

    transaction_id: UUID
    bound_at: datetime

    def __post_init__(self) -> None:
        _parse_strict_uuid(self.owner_id, "owner_id")
        _parse_strict_uuid(self.portfolio_id, "portfolio_id")
        _parse_strict_uuid(self.account_id, "account_id")
        _validate_strict_source_key(self.source_key, "source_key")
        _validate_strict_sha256_hex(self.file_content_sha256, "file_content_sha256")
        _parse_strict_record_ordinal(self.record_ordinal, "record_ordinal")
        _validate_strict_sha256_hex(self.record_sha256, "record_sha256")
        _validate_strict_sha256_hex(self.expected_plan_sha256, "expected_plan_sha256")
        _parse_strict_uuid(self.transaction_id, "transaction_id")
        _parse_strict_datetime(self.bound_at, "bound_at")

    @property
    def claim_identity(self) -> Tuple[UUID, UUID, str, str, int, str]:
        """
        Authoritative composite raw claim identity matching Phase 13O:
        (portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256).
        """
        return (
            self.portfolio_id,
            self.account_id,
            self.source_key,
            self.file_content_sha256,
            self.record_ordinal,
            self.record_sha256,
        )

    @property
    def interpreted_claim_identity(self) -> Tuple[UUID, UUID, str, str, int, str, str]:
        """
        Diagnostic interpreted claim tuple:
        (*claim_identity, expected_plan_sha256).
        """
        return (
            *self.claim_identity,
            self.expected_plan_sha256,
        )


def serialize_import_ledger_binding(
    intent: ImportLedgerBindingIntent,
    *,
    transaction_id: Union[UUID, str],
    expected_owner_id: Union[UUID, str],
) -> Dict[str, Any]:
    """
    Serializes an ImportLedgerBindingIntent and transaction reference into a database INSERT row payload.

    Args:
        intent: Authoritative ImportLedgerBindingIntent instance.
        transaction_id: Target ledger transaction UUID or canonical UUID string.
        expected_owner_id: Trusted portfolio owner UUID or canonical UUID string.

    Returns:
        Dictionary suitable for Supabase/PostgREST INSERT (bound_at is excluded as DB default).

    Raises:
        PortfolioImportCommitPersistenceError: If inputs are invalid or malformed.
    """
    if not isinstance(intent, ImportLedgerBindingIntent):
        raise PortfolioImportCommitPersistenceError(
            f"intent must be an ImportLedgerBindingIntent instance, got {type(intent).__name__}"
        )

    owner_uuid = _parse_strict_uuid(expected_owner_id, "expected_owner_id")
    tx_uuid = _parse_strict_uuid(transaction_id, "transaction_id")

    return {
        "owner_id": str(owner_uuid),
        "portfolio_id": str(intent.portfolio_id),
        "account_id": str(intent.account_id),
        "source_key": intent.source_key,
        "file_content_sha256": intent.file_content_sha256,
        "record_ordinal": intent.record_ordinal,
        "record_sha256": intent.record_sha256,
        "expected_plan_sha256": intent.expected_plan_sha256,
        "transaction_id": str(tx_uuid),
    }


def hydrate_import_ledger_binding(
    row: Dict[str, Any],
    *,
    expected_owner_id: Union[UUID, str],
) -> PersistedImportLedgerBinding:
    """
    Hydrates a database row from public.portfolio_import_claim_bindings into an immutable
    PersistedImportLedgerBinding instance.

    Args:
        row: Raw row dictionary from PostgreSQL/PostgREST.
        expected_owner_id: Trusted portfolio owner UUID or canonical UUID string for defense-in-depth verification.

    Returns:
        Immutable PersistedImportLedgerBinding instance.

    Raises:
        PortfolioImportCommitPersistenceError: If row is malformed, missing fields, or owner mismatch occurs.
    """
    if not isinstance(row, dict):
        raise PortfolioImportCommitPersistenceError(
            f"row must be a dict instance, got {type(row).__name__}"
        )

    required_keys = (
        "owner_id",
        "portfolio_id",
        "account_id",
        "source_key",
        "file_content_sha256",
        "record_ordinal",
        "record_sha256",
        "expected_plan_sha256",
        "transaction_id",
        "bound_at",
    )
    for key in required_keys:
        if key not in row:
            raise PortfolioImportCommitPersistenceError(f"Missing required column '{key}' in binding row.")

    trusted_owner = _parse_strict_uuid(expected_owner_id, "expected_owner_id")
    row_owner = _parse_strict_uuid(row["owner_id"], "owner_id")

    if row_owner != trusted_owner:
        raise PortfolioImportCommitPersistenceError(
            f"Owner isolation violation: row owner '{row_owner}' does not match expected owner '{trusted_owner}'."
        )

    portfolio_id = _parse_strict_uuid(row["portfolio_id"], "portfolio_id")
    account_id = _parse_strict_uuid(row["account_id"], "account_id")
    source_key = _validate_strict_source_key(row["source_key"], "source_key")
    file_content_sha256 = _validate_strict_sha256_hex(row["file_content_sha256"], "file_content_sha256")
    record_ordinal = _parse_strict_record_ordinal(row["record_ordinal"], "record_ordinal")
    record_sha256 = _validate_strict_sha256_hex(row["record_sha256"], "record_sha256")
    expected_plan_sha256 = _validate_strict_sha256_hex(row["expected_plan_sha256"], "expected_plan_sha256")
    transaction_id = _parse_strict_uuid(row["transaction_id"], "transaction_id")
    bound_at = _parse_strict_datetime(row["bound_at"], "bound_at")

    return PersistedImportLedgerBinding(
        owner_id=row_owner,
        portfolio_id=portfolio_id,
        account_id=account_id,
        source_key=source_key,
        file_content_sha256=file_content_sha256,
        record_ordinal=record_ordinal,
        record_sha256=record_sha256,
        expected_plan_sha256=expected_plan_sha256,
        transaction_id=transaction_id,
        bound_at=bound_at,
    )
