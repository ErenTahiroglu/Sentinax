"""
backend/engine/private/portfolio/fee_tax_attribution_history.py
===============================================================
Immutable Persisted Fee/Tax Attribution History Projection (Phase 14I).

This module provides a pure, deterministic, in-memory projection layer that:
1. Evaluates authoritative persisted attribution events at a system-knowledge cutoff (`as_of_recorded_at`).
2. Identifies all known attribution events (ALLOCATION and REVERSAL).
3. Resolves known reversal references to derive active allocation state.
4. Provides deterministic ordering and fast immutable lookup helpers.

Invariants:
- Pure Python domain logic: no network, no database, no Supabase, no SQL, no clock calls,
  no UUID generation, no hashing, no tax-law rules, no FX conversion, no ledger lookups.
- System-knowledge cutoff uses `recorded_at` physical UTC instants.
- Preserves exact caller-supplied `as_of_recorded_at` metadata representation.
- Preserves exact authoritative object identities across partitions (`is` comparison).
- Future-known reversals NEVER retroactively alter earlier PIT snapshots.
- Fail-closed on corrupted history (cross-portfolio events, duplicate physical event IDs,
  missing reversal targets, reversal of reversal, cross-account reversals, backdated reversals,
  or multiple reversals referencing the same allocation).
"""

from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
)


class FeeTaxAttributionHistoryError(ValueError):
    """Raised when fee/tax attribution history or projection validation fails closed."""
    pass


def _validate_uuid_instance(val: Any, field_name: str) -> UUID:
    """Validates that a field is an actual UUID instance (rejecting bool, str, int, etc.)."""
    if val is None or isinstance(val, bool) or not isinstance(val, UUID):
        raise FeeTaxAttributionHistoryError(
            f"Field '{field_name}' must be a UUID instance, got {type(val).__name__}: {val!r}"
        )
    return val


def _validate_optional_aware_datetime(dt: Any, field_name: str = "as_of_recorded_at") -> Optional[datetime]:
    """Validates that an optional datetime is timezone-aware with non-null utcoffset."""
    if dt is None:
        return None
    if isinstance(dt, bool) or not isinstance(dt, datetime):
        raise FeeTaxAttributionHistoryError(
            f"Field '{field_name}' must be a datetime instance or None, got {type(dt).__name__}: {dt!r}"
        )
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise FeeTaxAttributionHistoryError(
            f"Field '{field_name}' must be timezone-aware with non-null utcoffset, got naive: {dt!r}"
        )
    return dt


def _canonical_event_sort_key(event: FeeTaxAttributionPersistenceEvent) -> Tuple[datetime, str]:
    """Canonical ordering key: (recorded_at physical UTC instant, UUID string id)."""
    return (
        event.recorded_at.astimezone(timezone.utc),
        str(event.id),
    )


def _derive_history_partitions(
    portfolio_id: UUID,
    events: Sequence[FeeTaxAttributionPersistenceEvent],
    as_of_recorded_at: Optional[datetime],
) -> Tuple[
    Tuple[FeeTaxAttributionPersistenceEvent, ...],
    Tuple[FeeTaxAttributionPersistenceEvent, ...],
    Tuple[FeeTaxAttributionPersistenceEvent, ...],
    Tuple[FeeTaxAttributionPersistenceEvent, ...],
]:
    """
    Validates, filters by PIT cutoff, and derives canonical partitions from raw persistence events.
    """
    _validate_uuid_instance(portfolio_id, "portfolio_id")
    _validate_optional_aware_datetime(as_of_recorded_at, "as_of_recorded_at")

    if events is None or isinstance(events, (str, bytes)) or not isinstance(events, SequenceABC):
        raise FeeTaxAttributionHistoryError(
            f"events must be a non-string Sequence, got {type(events).__name__}: {events!r}"
        )

    seen_ids: set[UUID] = set()
    included_events: List[FeeTaxAttributionPersistenceEvent] = []

    cutoff_utc: Optional[datetime] = (
        as_of_recorded_at.astimezone(timezone.utc) if as_of_recorded_at is not None else None
    )

    for idx, e in enumerate(events):
        if e is None or isinstance(e, bool) or not isinstance(e, FeeTaxAttributionPersistenceEvent):
            raise FeeTaxAttributionHistoryError(
                f"Event at index {idx} must be a FeeTaxAttributionPersistenceEvent instance, got {type(e).__name__}: {e!r}"
            )
        if e.portfolio_id != portfolio_id:
            raise FeeTaxAttributionHistoryError(
                f"Event {e.id} belongs to portfolio {e.portfolio_id}, expected {portfolio_id}"
            )
        if e.id in seen_ids:
            raise FeeTaxAttributionHistoryError(
                f"Duplicate physical attribution event ID {e.id} found in input history"
            )
        seen_ids.add(e.id)

        # PIT filtering
        if cutoff_utc is not None:
            if e.recorded_at.astimezone(timezone.utc) > cutoff_utc:
                continue

        included_events.append(e)

    # Sort deterministically
    included_events.sort(key=_canonical_event_sort_key)
    sorted_events = tuple(included_events)

    allocations: List[FeeTaxAttributionPersistenceEvent] = []
    reversals: List[FeeTaxAttributionPersistenceEvent] = []
    events_by_id: Dict[UUID, FeeTaxAttributionPersistenceEvent] = {}

    for e in sorted_events:
        events_by_id[e.id] = e
        if e.event_type == FeeTaxAttributionEventType.ALLOCATION:
            allocations.append(e)
        elif e.event_type == FeeTaxAttributionEventType.REVERSAL:
            reversals.append(e)
        else:
            raise FeeTaxAttributionHistoryError(f"Unsupported event_type: {e.event_type}")

    # Validate reversals
    reversed_allocation_ids: Dict[UUID, FeeTaxAttributionPersistenceEvent] = {}
    for rev in reversals:
        target_id = rev.reverses_attribution_event_id
        if target_id is None:
            raise FeeTaxAttributionHistoryError(f"REVERSAL event {rev.id} has reverses_attribution_event_id=None")

        target_event = events_by_id.get(target_id)
        if target_event is None:
            raise FeeTaxAttributionHistoryError(
                f"REVERSAL event {rev.id} references attribution event {target_id} which is missing from supplied PIT history"
            )

        if target_event.event_type != FeeTaxAttributionEventType.ALLOCATION:
            raise FeeTaxAttributionHistoryError(
                f"REVERSAL event {rev.id} references event {target_id} with event_type {target_event.event_type}; reversal of reversal is prohibited"
            )

        if rev.portfolio_id != target_event.portfolio_id or rev.account_id != target_event.account_id:
            raise FeeTaxAttributionHistoryError(
                f"REVERSAL event {rev.id} (portfolio={rev.portfolio_id}, account={rev.account_id}) "
                f"mismatches referenced allocation {target_id} (portfolio={target_event.portfolio_id}, account={target_event.account_id})"
            )

        if rev.recorded_at.astimezone(timezone.utc) < target_event.recorded_at.astimezone(timezone.utc):
            raise FeeTaxAttributionHistoryError(
                f"Backdated REVERSAL event {rev.id} recorded_at ({rev.recorded_at}) precedes referenced allocation {target_id} recorded_at ({target_event.recorded_at})"
            )

        if target_id in reversed_allocation_ids:
            prior_rev = reversed_allocation_ids[target_id]
            raise FeeTaxAttributionHistoryError(
                f"Duplicate reversal for allocation {target_id}: already reversed by {prior_rev.id}, got second reversal {rev.id}"
            )
        reversed_allocation_ids[target_id] = rev


    active_allocations = tuple(
        alloc for alloc in allocations if alloc.id not in reversed_allocation_ids
    )

    return (
        sorted_events,
        tuple(allocations),
        tuple(reversals),
        active_allocations,
    )


@dataclass(frozen=True)
class PersistedFeeTaxAttributionHistoryView:
    """
    Immutable point-in-time projection of persisted fee/tax attribution event history.
    """
    portfolio_id: UUID
    as_of_recorded_at: Optional[datetime]

    events: Tuple[FeeTaxAttributionPersistenceEvent, ...]
    allocation_events: Tuple[FeeTaxAttributionPersistenceEvent, ...]
    reversal_events: Tuple[FeeTaxAttributionPersistenceEvent, ...]
    active_allocation_events: Tuple[FeeTaxAttributionPersistenceEvent, ...]

    def __post_init__(self) -> None:
        _validate_uuid_instance(self.portfolio_id, "portfolio_id")
        _validate_optional_aware_datetime(self.as_of_recorded_at, "as_of_recorded_at")

        # Rederive canonical state from self.events
        exp_events, exp_alloc, exp_rev, exp_active = _derive_history_partitions(
            self.portfolio_id,
            self.events,
            self.as_of_recorded_at,
        )

        # Verify exact length and object identity for events
        if len(self.events) != len(exp_events):
            raise FeeTaxAttributionHistoryError(
                f"Tampered events tuple length: got {len(self.events)}, expected {len(exp_events)}"
            )
        for idx, (actual, expected) in enumerate(zip(self.events, exp_events)):
            if actual is not expected:
                raise FeeTaxAttributionHistoryError(
                    f"Tampered event at index {idx}: object identity mismatch"
                )

        # Verify allocation_events
        if len(self.allocation_events) != len(exp_alloc):
            raise FeeTaxAttributionHistoryError(
                f"Tampered allocation_events length: got {len(self.allocation_events)}, expected {len(exp_alloc)}"
            )
        for idx, (actual, expected) in enumerate(zip(self.allocation_events, exp_alloc)):
            if actual is not expected:
                raise FeeTaxAttributionHistoryError(
                    f"Tampered allocation_event at index {idx}: object identity mismatch"
                )

        # Verify reversal_events
        if len(self.reversal_events) != len(exp_rev):
            raise FeeTaxAttributionHistoryError(
                f"Tampered reversal_events length: got {len(self.reversal_events)}, expected {len(exp_rev)}"
            )
        for idx, (actual, expected) in enumerate(zip(self.reversal_events, exp_rev)):
            if actual is not expected:
                raise FeeTaxAttributionHistoryError(
                    f"Tampered reversal_event at index {idx}: object identity mismatch"
                )

        # Verify active_allocation_events
        if len(self.active_allocation_events) != len(exp_active):
            raise FeeTaxAttributionHistoryError(
                f"Tampered active_allocation_events length: got {len(self.active_allocation_events)}, expected {len(exp_active)}"
            )
        for idx, (actual, expected) in enumerate(zip(self.active_allocation_events, exp_active)):
            if actual is not expected:
                raise FeeTaxAttributionHistoryError(
                    f"Tampered active_allocation_event at index {idx}: object identity mismatch"
                )

    def allocations_for_charge(
        self,
        charge_transaction_id: UUID,
    ) -> Tuple[FeeTaxAttributionPersistenceEvent, ...]:
        """Returns all known allocation events for a given charge transaction ID in canonical order."""
        c_id = _validate_uuid_instance(charge_transaction_id, "charge_transaction_id")
        return tuple(e for e in self.allocation_events if e.charge_transaction_id == c_id)

    def active_allocations_for_charge(
        self,
        charge_transaction_id: UUID,
    ) -> Tuple[FeeTaxAttributionPersistenceEvent, ...]:
        """Returns all active allocation events for a given charge transaction ID in canonical order."""
        c_id = _validate_uuid_instance(charge_transaction_id, "charge_transaction_id")
        return tuple(e for e in self.active_allocation_events if e.charge_transaction_id == c_id)

    def allocations_for_target(
        self,
        target_transaction_id: UUID,
    ) -> Tuple[FeeTaxAttributionPersistenceEvent, ...]:
        """Returns all known allocation events for a given target transaction ID in canonical order."""
        t_id = _validate_uuid_instance(target_transaction_id, "target_transaction_id")
        return tuple(e for e in self.allocation_events if e.target_transaction_id == t_id)

    def active_allocations_for_target(
        self,
        target_transaction_id: UUID,
    ) -> Tuple[FeeTaxAttributionPersistenceEvent, ...]:
        """Returns all active allocation events for a given target transaction ID in canonical order."""
        t_id = _validate_uuid_instance(target_transaction_id, "target_transaction_id")
        return tuple(e for e in self.active_allocation_events if e.target_transaction_id == t_id)

    def is_allocation_active(
        self,
        allocation_event_id: UUID,
    ) -> bool:
        """
        Returns True if allocation is active, False if reversed.
        Raises FeeTaxAttributionHistoryError if allocation_event_id is unknown in this view.
        """
        a_id = _validate_uuid_instance(allocation_event_id, "allocation_event_id")
        alloc_found = any(e.id == a_id for e in self.allocation_events)
        if not alloc_found:
            raise FeeTaxAttributionHistoryError(
                f"Unknown allocation event ID {a_id} in history view for portfolio {self.portfolio_id}"
            )
        return any(e.id == a_id for e in self.active_allocation_events)

    def reversal_for_allocation(
        self,
        allocation_event_id: UUID,
    ) -> Optional[FeeTaxAttributionPersistenceEvent]:
        """
        Returns the authoritative REVERSAL event reversing this allocation, or None if active.
        Raises FeeTaxAttributionHistoryError if allocation_event_id is unknown in this view.
        """
        a_id = _validate_uuid_instance(allocation_event_id, "allocation_event_id")
        alloc_found = any(e.id == a_id for e in self.allocation_events)
        if not alloc_found:
            raise FeeTaxAttributionHistoryError(
                f"Unknown allocation event ID {a_id} in history view for portfolio {self.portfolio_id}"
            )
        for rev in self.reversal_events:
            if rev.reverses_attribution_event_id == a_id:
                return rev
        return None


def build_persisted_fee_tax_attribution_history_view(
    portfolio_id: UUID,
    events: Sequence[FeeTaxAttributionPersistenceEvent],
    *,
    as_of_recorded_at: Optional[datetime] = None,
) -> PersistedFeeTaxAttributionHistoryView:
    """
    Constructs an immutable, reversal-aware point-in-time projection view of persisted attribution history.

    Args:
        portfolio_id: Authoritative UUID of the portfolio.
        events: Persisted sequence of FeeTaxAttributionPersistenceEvent records.
        as_of_recorded_at: Optional system-knowledge cutoff instant.

    Returns:
        PersistedFeeTaxAttributionHistoryView with canonical partitions and active allocation view.

    Raises:
        FeeTaxAttributionHistoryError: If arguments fail type or structural integrity checks.
    """
    sorted_events, alloc_events, rev_events, active_alloc_events = _derive_history_partitions(
        portfolio_id,
        events,
        as_of_recorded_at,
    )

    return PersistedFeeTaxAttributionHistoryView(
        portfolio_id=portfolio_id,
        as_of_recorded_at=as_of_recorded_at,
        events=sorted_events,
        allocation_events=alloc_events,
        reversal_events=rev_events,
        active_allocation_events=active_alloc_events,
    )
