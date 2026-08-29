"""
backend/engine/private/portfolio/fee_tax_attribution_persistence.py
==================================================================
Immutable Fee/Tax Attribution Persistence-Event Contract & Exact Codec (Phase 14E).

This module defines the immutable append-only event contract and exact row codec
for future durable fee/tax attribution evidence, without creating database tables,
RPCs, or repository write paths.

Key Architectural Invariants:
- Pure Python domain logic: no network, no Supabase, no SQL, no clock calls,
  no UUID generation, no hashlib, no tax rates, no legal rules, no FX conversion.
- Append-only event stream: corrections use REVERSAL events referencing prior ALLOCATION events;
  no UPDATE or DELETE semantics.
- Family validation:
  * ALLOCATION: charge_transaction_id (UUID), target_transaction_id (UUID),
    allocated_amount (finite Decimal > 0), reverses_attribution_event_id (None).
  * REVERSAL: charge_transaction_id (None), target_transaction_id (None),
    allocated_amount (None), reverses_attribution_event_id (UUID).
- Self-reversal rejected: id != reverses_attribution_event_id.
- Time axes: recorded_at is the system-knowledge time when attribution evidence was recorded.
- Defense-in-depth owner isolation: owner_id belongs to persistence serialization/hydration
  boundary and is NOT a field on domain events.
- Zero currency / transaction-type / instrument / date denormalization in attribution persistence events.
- Exact Decimal representation preservation across serialization and hydration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Dict, Mapping, Optional
from uuid import UUID

from backend.engine.private.portfolio.fee_tax_attribution import (
    ResolvedFeeTaxAttribution,
)

UUID_CANONICAL_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DECIMAL_CANONICAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

_REQUIRED_ROW_KEYS = frozenset({
    "id",
    "portfolio_id",
    "account_id",
    "owner_id",
    "event_type",
    "recorded_at",
    "charge_transaction_id",
    "target_transaction_id",
    "allocated_amount",
    "reverses_attribution_event_id",
})


class FeeTaxAttributionPersistenceError(ValueError):
    """Raised when fee/tax attribution persistence event or codec contract validation fails closed."""
    pass


class FeeTaxAttributionEventType(str, Enum):
    """Canonical event types for immutable fee/tax attribution persistence."""
    ALLOCATION = "allocation"
    REVERSAL = "reversal"


def _is_aware_datetime(dt: Any) -> bool:
    """Returns True if dt is a non-bool datetime instance with tzinfo and a non-None utcoffset."""
    if dt is None or isinstance(dt, bool) or not isinstance(dt, datetime):
        return False
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def _validate_uuid_instance(val: Any, field_name: str) -> UUID:
    """Validates that a field is an actual UUID instance."""
    if val is None:
        raise FeeTaxAttributionPersistenceError(f"Required UUID field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, UUID):
        raise FeeTaxAttributionPersistenceError(
            f"Field '{field_name}' must be a UUID instance, got {type(val).__name__}: {val!r}"
        )
    return val


def _validate_optional_uuid_instance(val: Any, field_name: str) -> Optional[UUID]:
    """Validates that a field is None or an actual UUID instance."""
    if val is None:
        return None
    return _validate_uuid_instance(val, field_name)


def _validate_aware_datetime_instance(val: Any, field_name: str = "recorded_at") -> datetime:
    """Validates that a field is an actual timezone-aware datetime instance."""
    if val is None:
        raise FeeTaxAttributionPersistenceError(f"Required datetime field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, datetime):
        raise FeeTaxAttributionPersistenceError(
            f"Field '{field_name}' must be a datetime instance, got {type(val).__name__}: {val!r}"
        )
    if not _is_aware_datetime(val):
        raise FeeTaxAttributionPersistenceError(
            f"Datetime field '{field_name}' must be timezone-aware with non-null utcoffset, got {val}"
        )
    return val


def _parse_canonical_uuid_string(val: Any, field_name: str) -> UUID:
    """
    Parses and strictly validates a canonical lowercase hyphenated UUID string from persisted row.
    Rejects UUID objects, uppercase, braces, hyphenless, whitespace, bool, int, etc.
    """
    if val is None:
        raise FeeTaxAttributionPersistenceError(f"Required UUID string field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, str):
        raise FeeTaxAttributionPersistenceError(
            f"Field '{field_name}' must be a canonical UUID str, got {type(val).__name__}: {val!r}"
        )
    if not UUID_CANONICAL_PATTERN.fullmatch(val):
        raise FeeTaxAttributionPersistenceError(
            f"Non-canonical or invalid UUID string for '{field_name}': {val!r}"
        )
    try:
        parsed = UUID(val)
        if str(parsed) != val:
            raise FeeTaxAttributionPersistenceError(
                f"Non-canonical UUID string representation for '{field_name}': {val!r}"
            )
        return parsed
    except Exception as e:
        raise FeeTaxAttributionPersistenceError(
            f"Invalid UUID string for '{field_name}': {val!r}"
        ) from e


def _parse_optional_canonical_uuid_string(val: Any, field_name: str) -> Optional[UUID]:
    """Parses an optional canonical lowercase hyphenated UUID string."""
    if val is None:
        return None
    return _parse_canonical_uuid_string(val, field_name)


def _parse_canonical_decimal_string(val: Any, field_name: str) -> Decimal:
    """
    Parses and strictly validates an exact decimal string from persisted row.
    Rejects Decimal instances, float, int, bool, NaN, Infinity, scientific notation, leading zeros.
    """
    if val is None:
        raise FeeTaxAttributionPersistenceError(f"Required Decimal string field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, str):
        raise FeeTaxAttributionPersistenceError(
            f"Field '{field_name}' must be an exact decimal str, got {type(val).__name__}: {val!r}"
        )
    if not DECIMAL_CANONICAL_PATTERN.fullmatch(val):
        raise FeeTaxAttributionPersistenceError(
            f"Non-canonical or invalid decimal string for '{field_name}': {val!r}"
        )
    try:
        dec = Decimal(val)
        if not dec.is_finite():
            raise FeeTaxAttributionPersistenceError(
                f"Non-finite Decimal rejected for '{field_name}': {val!r}"
            )
        return dec
    except Exception as e:
        if isinstance(e, FeeTaxAttributionPersistenceError):
            raise
        raise FeeTaxAttributionPersistenceError(
            f"Invalid decimal string for '{field_name}': {val!r}"
        ) from e


def _parse_canonical_datetime_string(val: Any, field_name: str = "recorded_at") -> datetime:
    """
    Parses an ISO-8601 timezone-aware datetime string from persisted row without normalization.
    """
    if val is None:
        raise FeeTaxAttributionPersistenceError(f"Required datetime string field '{field_name}' is missing or None.")
    if isinstance(val, bool) or not isinstance(val, str):
        raise FeeTaxAttributionPersistenceError(
            f"Field '{field_name}' must be an ISO-8601 datetime str, got {type(val).__name__}: {val!r}"
        )
    if not val.strip():
        raise FeeTaxAttributionPersistenceError(f"Datetime string '{field_name}' cannot be empty or whitespace.")
    try:
        dt = datetime.fromisoformat(val)
    except Exception as e:
        raise FeeTaxAttributionPersistenceError(
            f"Invalid ISO-8601 datetime string for '{field_name}': {val!r}"
        ) from e

    if not _is_aware_datetime(dt):
        raise FeeTaxAttributionPersistenceError(
            f"Datetime field '{field_name}' must be timezone-aware, got naive: {val!r}"
        )
    return dt


@dataclass(frozen=True)
class FeeTaxAttributionPersistenceEvent:
    """
    Immutable domain event representing a durable attribution record (ALLOCATION or REVERSAL).
    """
    id: UUID
    portfolio_id: UUID
    account_id: UUID
    event_type: FeeTaxAttributionEventType
    recorded_at: datetime
    charge_transaction_id: Optional[UUID] = None
    target_transaction_id: Optional[UUID] = None
    allocated_amount: Optional[Decimal] = None
    reverses_attribution_event_id: Optional[UUID] = None

    def __post_init__(self) -> None:
        _validate_uuid_instance(self.id, "id")
        _validate_uuid_instance(self.portfolio_id, "portfolio_id")
        _validate_uuid_instance(self.account_id, "account_id")
        _validate_aware_datetime_instance(self.recorded_at, "recorded_at")

        if isinstance(self.event_type, bool) or not isinstance(self.event_type, FeeTaxAttributionEventType):
            raise FeeTaxAttributionPersistenceError(
                f"event_type must be a FeeTaxAttributionEventType instance, got {type(self.event_type).__name__}: {self.event_type!r}"
            )

        if self.event_type == FeeTaxAttributionEventType.ALLOCATION:
            if self.charge_transaction_id is None:
                raise FeeTaxAttributionPersistenceError("ALLOCATION event requires non-None charge_transaction_id")
            _validate_uuid_instance(self.charge_transaction_id, "charge_transaction_id")

            if self.target_transaction_id is None:
                raise FeeTaxAttributionPersistenceError("ALLOCATION event requires non-None target_transaction_id")
            _validate_uuid_instance(self.target_transaction_id, "target_transaction_id")

            if self.charge_transaction_id == self.target_transaction_id:
                raise FeeTaxAttributionPersistenceError(
                    f"Self-attribution rejected: charge_transaction_id {self.charge_transaction_id} equals target_transaction_id"
                )

            if self.allocated_amount is None:
                raise FeeTaxAttributionPersistenceError("ALLOCATION event requires non-None allocated_amount")
            if isinstance(self.allocated_amount, bool) or not isinstance(self.allocated_amount, Decimal):
                raise FeeTaxAttributionPersistenceError(
                    f"allocated_amount must be a Decimal instance, got {type(self.allocated_amount).__name__}: {self.allocated_amount!r}"
                )
            if not self.allocated_amount.is_finite() or self.allocated_amount <= Decimal("0"):
                raise FeeTaxAttributionPersistenceError(
                    f"allocated_amount must be a finite strictly positive Decimal (> 0), got {self.allocated_amount}"
                )

            if self.reverses_attribution_event_id is not None:
                raise FeeTaxAttributionPersistenceError(
                    f"ALLOCATION event must have reverses_attribution_event_id=None, got {self.reverses_attribution_event_id}"
                )

        elif self.event_type == FeeTaxAttributionEventType.REVERSAL:
            if self.charge_transaction_id is not None:
                raise FeeTaxAttributionPersistenceError(
                    f"REVERSAL event must have charge_transaction_id=None, got {self.charge_transaction_id}"
                )
            if self.target_transaction_id is not None:
                raise FeeTaxAttributionPersistenceError(
                    f"REVERSAL event must have target_transaction_id=None, got {self.target_transaction_id}"
                )
            if self.allocated_amount is not None:
                raise FeeTaxAttributionPersistenceError(
                    f"REVERSAL event must have allocated_amount=None, got {self.allocated_amount}"
                )

            if self.reverses_attribution_event_id is None:
                raise FeeTaxAttributionPersistenceError("REVERSAL event requires non-None reverses_attribution_event_id")
            _validate_uuid_instance(self.reverses_attribution_event_id, "reverses_attribution_event_id")

            if self.reverses_attribution_event_id == self.id:
                raise FeeTaxAttributionPersistenceError(
                    f"Self-reversal rejected: reverses_attribution_event_id {self.reverses_attribution_event_id} equals event id"
                )
        else:
            raise FeeTaxAttributionPersistenceError(f"Unsupported event_type: {self.event_type}")


def build_allocation_persistence_event(
    *,
    event_id: UUID,
    recorded_at: datetime,
    attribution: ResolvedFeeTaxAttribution,
) -> FeeTaxAttributionPersistenceEvent:
    """
    Builds an immutable ALLOCATION persistence event from an authoritative ResolvedFeeTaxAttribution.

    Args:
        event_id: Caller/repository-generated UUID for the new persistence event.
        recorded_at: Caller/repository-supplied system-knowledge timestamp (must be timezone-aware).
        attribution: Authoritative ResolvedFeeTaxAttribution from Phase 14D.

    Returns:
        FeeTaxAttributionPersistenceEvent of type ALLOCATION.

    Raises:
        FeeTaxAttributionPersistenceError: If arguments fail type or domain validation.
    """
    _validate_uuid_instance(event_id, "event_id")
    _validate_aware_datetime_instance(recorded_at, "recorded_at")

    if isinstance(attribution, bool) or not isinstance(attribution, ResolvedFeeTaxAttribution):
        raise FeeTaxAttributionPersistenceError(
            f"attribution must be a ResolvedFeeTaxAttribution instance, got {type(attribution).__name__}: {attribution!r}"
        )

    return FeeTaxAttributionPersistenceEvent(
        id=event_id,
        portfolio_id=attribution.charge_transaction.portfolio_id,
        account_id=attribution.charge_transaction.account_id,
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        recorded_at=recorded_at,
        charge_transaction_id=attribution.charge_transaction.id,
        target_transaction_id=attribution.target_transaction.id,
        allocated_amount=attribution.allocated_amount,
        reverses_attribution_event_id=None,
    )


def build_attribution_reversal_persistence_event(
    *,
    event_id: UUID,
    portfolio_id: UUID,
    account_id: UUID,
    recorded_at: datetime,
    reverses_attribution_event_id: UUID,
) -> FeeTaxAttributionPersistenceEvent:
    """
    Builds an immutable REVERSAL persistence event referencing a prior attribution event ID.

    Args:
        event_id: Caller/repository-generated UUID for the new reversal persistence event.
        portfolio_id: Authoritative UUID of the portfolio.
        account_id: Authoritative UUID of the account.
        recorded_at: Caller/repository-supplied system-knowledge timestamp (must be timezone-aware).
        reverses_attribution_event_id: UUID of the attribution event being reversed.

    Returns:
        FeeTaxAttributionPersistenceEvent of type REVERSAL.

    Raises:
        FeeTaxAttributionPersistenceError: If arguments fail type or domain validation.
    """
    _validate_uuid_instance(event_id, "event_id")
    _validate_uuid_instance(portfolio_id, "portfolio_id")
    _validate_uuid_instance(account_id, "account_id")
    _validate_aware_datetime_instance(recorded_at, "recorded_at")
    _validate_uuid_instance(reverses_attribution_event_id, "reverses_attribution_event_id")

    return FeeTaxAttributionPersistenceEvent(
        id=event_id,
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.REVERSAL,
        recorded_at=recorded_at,
        charge_transaction_id=None,
        target_transaction_id=None,
        allocated_amount=None,
        reverses_attribution_event_id=reverses_attribution_event_id,
    )


def serialize_fee_tax_attribution_persistence_event(
    event: FeeTaxAttributionPersistenceEvent,
    owner_id: UUID,
) -> Dict[str, Any]:
    """
    Serializes an immutable FeeTaxAttributionPersistenceEvent to a database-shaped dictionary row.

    Args:
        event: Authoritative FeeTaxAttributionPersistenceEvent instance.
        owner_id: Authoritative owner UUID (persistence security context).

    Returns:
        Dictionary containing exactly the 10 canonical persistence keys.

    Raises:
        FeeTaxAttributionPersistenceError: If event or owner_id fails type validation.
    """
    if isinstance(event, bool) or not isinstance(event, FeeTaxAttributionPersistenceEvent):
        raise FeeTaxAttributionPersistenceError(
            f"event must be a FeeTaxAttributionPersistenceEvent instance, got {type(event).__name__}: {event!r}"
        )
    _validate_uuid_instance(owner_id, "owner_id")

    return {
        "id": str(event.id),
        "portfolio_id": str(event.portfolio_id),
        "account_id": str(event.account_id),
        "owner_id": str(owner_id),
        "event_type": event.event_type.value,
        "recorded_at": event.recorded_at.isoformat(),
        "charge_transaction_id": str(event.charge_transaction_id) if event.charge_transaction_id is not None else None,
        "target_transaction_id": str(event.target_transaction_id) if event.target_transaction_id is not None else None,
        "allocated_amount": str(event.allocated_amount) if event.allocated_amount is not None else None,
        "reverses_attribution_event_id": str(event.reverses_attribution_event_id) if event.reverses_attribution_event_id is not None else None,
    }


def hydrate_fee_tax_attribution_persistence_event(
    row: Mapping[str, Any],
    expected_owner_id: UUID,
) -> FeeTaxAttributionPersistenceEvent:
    """
    Hydrates and strictly validates a persisted database row dictionary into a
    FeeTaxAttributionPersistenceEvent domain entity.

    Args:
        row: Mapping containing persisted database row fields.
        expected_owner_id: Expected trusted owner UUID for defense-in-depth validation.

    Returns:
        FeeTaxAttributionPersistenceEvent domain aggregate.

    Raises:
        FeeTaxAttributionPersistenceError: If row fails schema, owner, type, or domain validation.
    """
    _validate_uuid_instance(expected_owner_id, "expected_owner_id")

    if row is None or isinstance(row, (str, bytes, list, tuple)) or not isinstance(row, Mapping):
        raise FeeTaxAttributionPersistenceError(
            f"Persisted row must be a Mapping, got {type(row).__name__}: {row!r}"
        )

    row_keys = set(row.keys())
    if row_keys != _REQUIRED_ROW_KEYS:
        missing = _REQUIRED_ROW_KEYS - row_keys
        extra = row_keys - _REQUIRED_ROW_KEYS
        error_parts = []
        if missing:
            error_parts.append(f"missing required keys: {sorted(missing)}")
        if extra:
            error_parts.append(f"unexpected extra keys: {sorted(extra)}")
        raise FeeTaxAttributionPersistenceError(
            f"Persisted row schema mismatch: {'; '.join(error_parts)}"
        )

    # Validate owner_id
    owner_str = row["owner_id"]
    parsed_owner = _parse_canonical_uuid_string(owner_str, "owner_id")
    if parsed_owner != expected_owner_id:
        raise FeeTaxAttributionPersistenceError(
            f"Row owner_id {parsed_owner} does not match expected_owner_id {expected_owner_id}"
        )

    event_id = _parse_canonical_uuid_string(row["id"], "id")
    portfolio_id = _parse_canonical_uuid_string(row["portfolio_id"], "portfolio_id")
    account_id = _parse_canonical_uuid_string(row["account_id"], "account_id")

    event_type_raw = row["event_type"]
    if isinstance(event_type_raw, bool) or not isinstance(event_type_raw, str):
        raise FeeTaxAttributionPersistenceError(
            f"event_type must be a string, got {type(event_type_raw).__name__}: {event_type_raw!r}"
        )
    if event_type_raw == FeeTaxAttributionEventType.ALLOCATION.value:
        event_type = FeeTaxAttributionEventType.ALLOCATION
    elif event_type_raw == FeeTaxAttributionEventType.REVERSAL.value:
        event_type = FeeTaxAttributionEventType.REVERSAL
    else:
        raise FeeTaxAttributionPersistenceError(
            f"Unknown or invalid event_type string in persisted row: {event_type_raw!r}"
        )

    recorded_at = _parse_canonical_datetime_string(row["recorded_at"], "recorded_at")

    charge_tx_id = _parse_optional_canonical_uuid_string(row["charge_transaction_id"], "charge_transaction_id")
    target_tx_id = _parse_optional_canonical_uuid_string(row["target_transaction_id"], "target_transaction_id")

    allocated_amount_raw = row["allocated_amount"]
    allocated_amount: Optional[Decimal] = None
    if allocated_amount_raw is not None:
        allocated_amount = _parse_canonical_decimal_string(allocated_amount_raw, "allocated_amount")

    reverses_event_id = _parse_optional_canonical_uuid_string(
        row["reverses_attribution_event_id"],
        "reverses_attribution_event_id",
    )

    return FeeTaxAttributionPersistenceEvent(
        id=event_id,
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=event_type,
        recorded_at=recorded_at,
        charge_transaction_id=charge_tx_id,
        target_transaction_id=target_tx_id,
        allocated_amount=allocated_amount,
        reverses_attribution_event_id=reverses_event_id,
    )
