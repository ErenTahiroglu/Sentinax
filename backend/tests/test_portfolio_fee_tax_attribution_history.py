"""
backend/tests/test_portfolio_fee_tax_attribution_history.py
===========================================================
Tests for Phase 14I: Immutable Persisted Fee/Tax Attribution History Projection.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import inspect
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio.fee_tax_attribution_history import (
    FeeTaxAttributionHistoryError,
    PersistedFeeTaxAttributionHistoryView,
    build_persisted_fee_tax_attribution_history_view,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
)


def make_allocation_event(
    portfolio_id: UUID,
    account_id: UUID,
    event_id: UUID | None = None,
    charge_transaction_id: UUID | None = None,
    target_transaction_id: UUID | None = None,
    allocated_amount: Decimal = Decimal("50.000"),
    recorded_at: datetime | None = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        recorded_at=recorded_at or datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc),
        charge_transaction_id=charge_transaction_id or uuid4(),
        target_transaction_id=target_transaction_id or uuid4(),
        allocated_amount=allocated_amount,
        reverses_attribution_event_id=None,
    )


def make_reversal_event(
    portfolio_id: UUID,
    account_id: UUID,
    reverses_attribution_event_id: UUID,
    event_id: UUID | None = None,
    recorded_at: datetime | None = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.REVERSAL,
        recorded_at=recorded_at or datetime(2026, 8, 29, 13, 0, 0, tzinfo=timezone.utc),
        charge_transaction_id=None,
        target_transaction_id=None,
        allocated_amount=None,
        reverses_attribution_event_id=reverses_attribution_event_id,
    )


class TestPersistedFeeTaxAttributionHistoryProjection:
    """Unit and domain tests for build_persisted_fee_tax_attribution_history_view."""

    def test_empty_history(self):
        """Item 46: Empty event sequence yields empty tuples."""
        p_id = uuid4()
        view = build_persisted_fee_tax_attribution_history_view(p_id, [])
        assert view.portfolio_id == p_id
        assert view.as_of_recorded_at is None
        assert view.events == ()
        assert view.allocation_events == ()
        assert view.reversal_events == ()
        assert view.active_allocation_events == ()

    def test_single_active_allocation(self):
        """Item 47: Single allocation is active and included in active_allocation_events."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id)

        view = build_persisted_fee_tax_attribution_history_view(p_id, [alloc])
        assert view.events == (alloc,)
        assert view.allocation_events == (alloc,)
        assert view.reversal_events == ()
        assert view.active_allocation_events == (alloc,)
        assert view.is_allocation_active(alloc.id) is True
        assert view.reversal_for_allocation(alloc.id) is None

    def test_reversed_allocation(self):
        """Item 48: Allocation paired with reversal is inactive."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))
        rev = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 13, 0, 0, tzinfo=timezone.utc))

        view = build_persisted_fee_tax_attribution_history_view(p_id, [alloc, rev])
        assert view.events == (alloc, rev)
        assert view.allocation_events == (alloc,)
        assert view.reversal_events == (rev,)
        assert view.active_allocation_events == ()
        assert view.is_allocation_active(alloc.id) is False
        assert view.reversal_for_allocation(alloc.id) is rev

    def test_future_reversal_pit_semantics(self):
        """Item 49: Reversal after cutoff does not affect earlier PIT snapshot."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        rev = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 14, 0, 0, tzinfo=timezone.utc))

        # Cutoff at 12:00 -> alloc active, rev excluded
        view_early = build_persisted_fee_tax_attribution_history_view(
            p_id, [alloc, rev], as_of_recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        )
        assert view_early.events == (alloc,)
        assert view_early.active_allocation_events == (alloc,)
        assert view_early.is_allocation_active(alloc.id) is True

        # Cutoff at 15:00 -> both included, alloc inactive
        view_late = build_persisted_fee_tax_attribution_history_view(
            p_id, [alloc, rev], as_of_recorded_at=datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone.utc)
        )
        assert view_late.events == (alloc, rev)
        assert view_late.active_allocation_events == ()
        assert view_late.is_allocation_active(alloc.id) is False

    def test_same_time_reversal_pit_semantics(self):
        """Item 50: Allocation and reversal at same timestamp are both included and allocation is inactive."""
        p_id = uuid4()
        a_id = uuid4()
        t = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        alloc = make_allocation_event(p_id, a_id, recorded_at=t)
        rev = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=t)

        view = build_persisted_fee_tax_attribution_history_view(p_id, [alloc, rev], as_of_recorded_at=t)
        assert view.events == (alloc, rev) or view.events == (rev, alloc)
        assert view.allocation_events == (alloc,)
        assert view.reversal_events == (rev,)
        assert view.active_allocation_events == ()
        assert view.is_allocation_active(alloc.id) is False

    def test_arbitrary_input_order_deterministic(self):
        """Item 51: Scrambled input produces deterministic (recorded_at, id) order."""
        p_id = uuid4()
        a_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 8, 29, 14, 0, 0, tzinfo=timezone.utc)

        e1 = make_allocation_event(p_id, a_id, recorded_at=t1)
        e2 = make_allocation_event(p_id, a_id, recorded_at=t2)
        e3 = make_allocation_event(p_id, a_id, recorded_at=t3)

        # Pass in reverse order
        view = build_persisted_fee_tax_attribution_history_view(p_id, [e3, e1, e2])
        assert view.events == (e1, e2, e3)

    def test_same_timestamp_uuid_ordering(self):
        """Item 52: Multiple events with same recorded_at use id tie-break."""
        p_id = uuid4()
        a_id = uuid4()
        t = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

        id1 = UUID("11111111-1111-4111-8111-111111111111")
        id2 = UUID("22222222-2222-4222-8222-222222222222")

        e1 = make_allocation_event(p_id, a_id, event_id=id1, recorded_at=t)
        e2 = make_allocation_event(p_id, a_id, event_id=id2, recorded_at=t)

        view = build_persisted_fee_tax_attribution_history_view(p_id, [e2, e1])
        assert view.events == (e1, e2)

    def test_cross_portfolio_rejection(self):
        """Item 53: Event from different portfolio raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        other_p_id = uuid4()
        a_id = uuid4()
        e = make_allocation_event(other_p_id, a_id)

        with pytest.raises(FeeTaxAttributionHistoryError, match="belongs to portfolio"):
            build_persisted_fee_tax_attribution_history_view(p_id, [e])

    def test_duplicate_event_id_rejection(self):
        """Item 54: Duplicate event ID in input raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        a_id = uuid4()
        e_id = uuid4()
        e1 = make_allocation_event(p_id, a_id, event_id=e_id)
        e2 = make_allocation_event(p_id, a_id, event_id=e_id)

        with pytest.raises(FeeTaxAttributionHistoryError, match="Duplicate physical attribution event ID"):
            build_persisted_fee_tax_attribution_history_view(p_id, [e1, e2])

    def test_missing_reversal_target_rejection(self):
        """Item 55: Reversal referencing a missing allocation raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        a_id = uuid4()
        missing_target_id = uuid4()
        rev = make_reversal_event(p_id, a_id, reverses_attribution_event_id=missing_target_id)

        with pytest.raises(FeeTaxAttributionHistoryError, match="is missing from supplied PIT history"):
            build_persisted_fee_tax_attribution_history_view(p_id, [rev])

    def test_reversal_of_reversal_rejection(self):
        """Item 56: Reversal referencing another REVERSAL raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        rev1 = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))
        rev2 = make_reversal_event(p_id, a_id, reverses_attribution_event_id=rev1.id, recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(FeeTaxAttributionHistoryError, match="reversal of reversal is prohibited"):
            build_persisted_fee_tax_attribution_history_view(p_id, [alloc, rev1, rev2])

    def test_cross_account_reversal_rejection(self):
        """Item 57: Reversal with different account_id than allocation raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        a_id_1 = uuid4()
        a_id_2 = uuid4()
        alloc = make_allocation_event(p_id, a_id_1, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        rev = make_reversal_event(p_id, a_id_2, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(FeeTaxAttributionHistoryError, match="mismatches referenced allocation"):
            build_persisted_fee_tax_attribution_history_view(p_id, [alloc, rev])

    def test_backdated_reversal_rejection(self):
        """Item 58: Reversal recorded before allocation raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))
        rev = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(FeeTaxAttributionHistoryError, match="Backdated REVERSAL event"):
            build_persisted_fee_tax_attribution_history_view(p_id, [alloc, rev])

    def test_double_reversal_rejection(self):
        """Item 59: Two reversals for the same allocation raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        rev1 = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))
        rev2 = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(FeeTaxAttributionHistoryError, match="Duplicate reversal for allocation"):
            build_persisted_fee_tax_attribution_history_view(p_id, [alloc, rev1, rev2])

    def test_physical_pit_comparison(self):
        """Item 60: Event at 12:00+00:00 included by cutoff at 15:00+03:00."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        cutoff = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(datetime.fromisoformat("2026-08-29T15:00:00+03:00").tzinfo.utcoffset(None)))
        view = build_persisted_fee_tax_attribution_history_view(p_id, [alloc], as_of_recorded_at=cutoff)
        assert view.events == (alloc,)

    def test_cutoff_representation_preservation(self):
        """Item 61: Exact caller-supplied cutoff representation is preserved in view metadata."""
        p_id = uuid4()
        cutoff = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(datetime.fromisoformat("2026-08-29T15:00:00+03:00").tzinfo.utcoffset(None)))
        view = build_persisted_fee_tax_attribution_history_view(p_id, [], as_of_recorded_at=cutoff)
        assert view.as_of_recorded_at is cutoff
        assert view.as_of_recorded_at.tzinfo.utcoffset(None).total_seconds() == 3 * 3600

    def test_helper_queries_and_ordering(self):
        """Item 62: Charge/target query helpers retain canonical allocation order."""
        p_id = uuid4()
        a_id = uuid4()
        charge_id_1 = uuid4()
        charge_id_2 = uuid4()
        target_id_1 = uuid4()
        target_id_2 = uuid4()

        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        t4 = datetime(2026, 8, 29, 13, 0, 0, tzinfo=timezone.utc)

        # alloc1: C1 -> T1
        alloc1 = make_allocation_event(p_id, a_id, charge_transaction_id=charge_id_1, target_transaction_id=target_id_1, recorded_at=t1)
        # alloc2: C1 -> T2 (reversed later)
        alloc2 = make_allocation_event(p_id, a_id, charge_transaction_id=charge_id_1, target_transaction_id=target_id_2, recorded_at=t2)
        # alloc3: C2 -> T1
        alloc3 = make_allocation_event(p_id, a_id, charge_transaction_id=charge_id_2, target_transaction_id=target_id_1, recorded_at=t3)
        # rev: reverses alloc2
        rev2 = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc2.id, recorded_at=t4)

        view = build_persisted_fee_tax_attribution_history_view(p_id, [alloc1, alloc2, alloc3, rev2])

        # Charge helpers
        assert view.allocations_for_charge(charge_id_1) == (alloc1, alloc2)
        assert view.active_allocations_for_charge(charge_id_1) == (alloc1,)
        assert view.allocations_for_charge(charge_id_2) == (alloc3,)
        assert view.active_allocations_for_charge(charge_id_2) == (alloc3,)

        # Target helpers
        assert view.allocations_for_target(target_id_1) == (alloc1, alloc3)
        assert view.active_allocations_for_target(target_id_1) == (alloc1, alloc3)
        assert view.allocations_for_target(target_id_2) == (alloc2,)
        assert view.active_allocations_for_target(target_id_2) == ()

    def test_unknown_allocation_activity_id_raises(self):
        """Item 63: is_allocation_active on unknown ID raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        view = build_persisted_fee_tax_attribution_history_view(p_id, [])
        with pytest.raises(FeeTaxAttributionHistoryError, match="Unknown allocation event ID"):
            view.is_allocation_active(uuid4())

    def test_unknown_reversal_lookup_id_raises(self):
        """Item 64: reversal_for_allocation on unknown ID raises FeeTaxAttributionHistoryError."""
        p_id = uuid4()
        view = build_persisted_fee_tax_attribution_history_view(p_id, [])
        with pytest.raises(FeeTaxAttributionHistoryError, match="Unknown allocation event ID"):
            view.reversal_for_allocation(uuid4())

    def test_direct_constructor_wrong_active_set_rejected(self):
        """Item 65: Direct constructor with forged active set is rejected."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        rev = make_reversal_event(p_id, a_id, reverses_attribution_event_id=alloc.id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))

        # Attempt to forge active_allocation_events=(alloc,) when it was reversed
        with pytest.raises(FeeTaxAttributionHistoryError, match="Tampered active_allocation_events length"):
            PersistedFeeTaxAttributionHistoryView(
                portfolio_id=p_id,
                as_of_recorded_at=None,
                events=(alloc, rev),
                allocation_events=(alloc,),
                reversal_events=(rev,),
                active_allocation_events=(alloc,),  # Forged!
            )

    def test_direct_constructor_reordered_events_rejected(self):
        """Item 66: Direct constructor with reordered events tuple is rejected."""
        p_id = uuid4()
        a_id = uuid4()
        e1 = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        e2 = make_allocation_event(p_id, a_id, recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(FeeTaxAttributionHistoryError, match="Tampered event at index"):
            PersistedFeeTaxAttributionHistoryView(
                portfolio_id=p_id,
                as_of_recorded_at=None,
                events=(e2, e1),  # Wrong order!
                allocation_events=(e1, e2),
                reversal_events=(),
                active_allocation_events=(e1, e2),
            )

    def test_direct_constructor_semantic_copy_rejected(self):
        """Item 67: Replacing authoritative event object with equal copied event is rejected."""
        p_id = uuid4()
        a_id = uuid4()
        alloc = make_allocation_event(p_id, a_id)
        alloc_copy = replace(alloc)  # Equal in ==, but not alloc_copy is alloc

        with pytest.raises(FeeTaxAttributionHistoryError, match="object identity mismatch"):
            PersistedFeeTaxAttributionHistoryView(
                portfolio_id=p_id,
                as_of_recorded_at=None,
                events=(alloc_copy,),
                allocation_events=(alloc,),
                reversal_events=(),
                active_allocation_events=(alloc,),
            )


class TestStaticPurity:
    """Item 68: Verify production code contains zero prohibited patterns."""

    def test_no_prohibited_imports_or_calls(self):
        import backend.engine.private.portfolio.fee_tax_attribution_history as mod
        source = inspect.getsource(mod)

        prohibited = [
            "datetime.now",
            "datetime.utcnow",
            "date.today",
            "uuid4",
            "uuid5",
            "hashlib",
            "sha256",
            ".rpc(",
            ".table(",
            "PortfolioRepository",
            "float(",
            "round(",
            "quantize(",
        ]
        for p in prohibited:
            assert p not in source, f"Found prohibited pattern '{p}' in fee_tax_attribution_history.py"
