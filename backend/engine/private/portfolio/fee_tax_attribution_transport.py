"""
backend/engine/private/portfolio/fee_tax_attribution_transport.py
=================================================================
Exact PostgREST Transport Adapter for Persisted Fee/Tax Attribution Events (Phase 14H).

This module adapts raw PostgREST dictionary responses into exact canonical rows
for the Phase 14E strict hydrator (`hydrate_fee_tax_attribution_persistence_event`).

Invariants:
- Rejects missing or extra keys against the exact 10-key contract.
- Rejects JSON floats, ints, Decimals, or booleans for allocated_amount (must be str or None).
- Rejects naive, non-string, or invalid datetime types for recorded_at.
- Converts timezone-aware PostgREST timestamps to canonical UTC Python isoformat (fold=0).
- Preserves exact microsecond precision and Decimal string representations without loss.
- Leaves UUID and event_type validation to the Phase 14E canonical hydrator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Final, FrozenSet, Mapping

_REQUIRED_ATTRIBUTION_ROW_KEYS: Final[FrozenSet[str]] = frozenset({
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


class FeeTaxAttributionTransportError(ValueError):
    """Raised when raw PostgREST fee/tax attribution transport data violates transport invariants."""
    pass


def canonicalize_fee_tax_attribution_postgrest_row(
    row: Mapping[str, Any],
) -> Dict[str, Any]:
    """
    Normalizes a raw PostgREST dictionary row into the exact canonical format
    required by Phase 14E `hydrate_fee_tax_attribution_persistence_event`.

    Args:
        row: Raw Mapping row returned by PostgREST select query.

    Returns:
        Dict[str, Any] containing exactly the 10 canonical persistence keys with
        recorded_at canonicalized to UTC ISO format.

    Raises:
        FeeTaxAttributionTransportError: If row structure, types, or timestamps fail validation.
    """
    if row is None or isinstance(row, (str, bytes, list, tuple, set)) or not isinstance(row, Mapping):
        raise FeeTaxAttributionTransportError(
            f"PostgREST row must be a Mapping, got {type(row).__name__}: {row!r}"
        )

    row_keys = set(row.keys())
    if row_keys != _REQUIRED_ATTRIBUTION_ROW_KEYS:
        missing = _REQUIRED_ATTRIBUTION_ROW_KEYS - row_keys
        extra = row_keys - _REQUIRED_ATTRIBUTION_ROW_KEYS
        error_parts = []
        if missing:
            error_parts.append(f"missing required keys: {sorted(missing)}")
        if extra:
            error_parts.append(f"unexpected extra keys: {sorted(extra)}")
        raise FeeTaxAttributionTransportError(
            f"PostgREST attribution row schema mismatch: {'; '.join(error_parts)}"
        )

    # Validate allocated_amount transport type
    allocated_amount = row["allocated_amount"]
    if allocated_amount is not None:
        if isinstance(allocated_amount, bool) or not isinstance(allocated_amount, str):
            raise FeeTaxAttributionTransportError(
                f"allocated_amount must be str or None from PostgREST text-cast select, "
                f"got {type(allocated_amount).__name__}: {allocated_amount!r}"
            )

    # Validate recorded_at transport type
    raw_recorded_at = row["recorded_at"]
    if raw_recorded_at is None or isinstance(raw_recorded_at, bool) or not isinstance(raw_recorded_at, str):
        raise FeeTaxAttributionTransportError(
            f"recorded_at must be an ISO datetime string from PostgREST, "
            f"got {type(raw_recorded_at).__name__}: {raw_recorded_at!r}"
        )

    if not raw_recorded_at.strip():
        raise FeeTaxAttributionTransportError("recorded_at string cannot be empty or whitespace.")

    try:
        parsed_dt = datetime.fromisoformat(raw_recorded_at)
    except Exception as e:
        raise FeeTaxAttributionTransportError(
            f"Invalid ISO-8601 datetime string for recorded_at: {raw_recorded_at!r}"
        ) from e

    if parsed_dt.tzinfo is None or parsed_dt.tzinfo.utcoffset(parsed_dt) is None:
        raise FeeTaxAttributionTransportError(
            f"recorded_at must be timezone-aware from PostgREST, got naive: {raw_recorded_at!r}"
        )

    canonical_dt = parsed_dt.astimezone(timezone.utc)
    if canonical_dt.fold != 0:
        raise FeeTaxAttributionTransportError(
            f"canonical recorded_at must have fold=0, got {canonical_dt.fold}"
        )

    canonical_recorded_at_str = canonical_dt.isoformat()

    return {
        "id": row["id"],
        "portfolio_id": row["portfolio_id"],
        "account_id": row["account_id"],
        "owner_id": row["owner_id"],
        "event_type": row["event_type"],
        "recorded_at": canonical_recorded_at_str,
        "charge_transaction_id": row["charge_transaction_id"],
        "target_transaction_id": row["target_transaction_id"],
        "allocated_amount": allocated_amount,
        "reverses_attribution_event_id": row["reverses_attribution_event_id"],
    }
