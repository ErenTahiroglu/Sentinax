"""
backend/tests/test_portfolio_fee_tax_attribution_transport.py
=============================================================
Tests for Phase 14H: PostgREST Transport Adapter for Persisted Fee/Tax Attribution Events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
    hydrate_fee_tax_attribution_persistence_event,
)
from backend.engine.private.portfolio.fee_tax_attribution_transport import (
    FeeTaxAttributionTransportError,
    canonicalize_fee_tax_attribution_postgrest_row,
)


def make_valid_postgrest_allocation_row(
    event_id: str = "a1111111-1111-4111-8111-111111111111",
    portfolio_id: str = "b2222222-2222-4222-8222-222222222222",
    account_id: str = "c3333333-3333-4333-8333-333333333333",
    owner_id: str = "d4444444-4444-4444-8444-444444444444",
    event_type: str = "allocation",
    recorded_at: str = "2026-08-29T12:00:00+00:00",
    charge_transaction_id: str = "e5555555-5555-4555-8555-555555555555",
    target_transaction_id: str = "f6666666-6666-4666-8666-666666666666",
    allocated_amount: str = "100.000",
    reverses_attribution_event_id: str | None = None,
) -> dict:
    return {
        "id": event_id,
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "owner_id": owner_id,
        "event_type": event_type,
        "recorded_at": recorded_at,
        "charge_transaction_id": charge_transaction_id,
        "target_transaction_id": target_transaction_id,
        "allocated_amount": allocated_amount,
        "reverses_attribution_event_id": reverses_attribution_event_id,
    }


class TestFeeTaxAttributionTransport:
    """Unit tests for canonicalize_fee_tax_attribution_postgrest_row."""

    def test_decimal_string_exact_preservation(self):
        """Item 42: Allocated amount string preserved exactly and hydrated to exact Decimal."""
        owner_id = UUID("d4444444-4444-4444-8444-444444444444")
        row = make_valid_postgrest_allocation_row(allocated_amount="6.000")
        canonical = canonicalize_fee_tax_attribution_postgrest_row(row)
        assert canonical["allocated_amount"] == "6.000"

        event = hydrate_fee_tax_attribution_persistence_event(canonical, expected_owner_id=owner_id)
        assert event.allocated_amount == Decimal("6.000")
        assert event.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()

    def test_float_rejection_at_transport_boundary(self):
        """Item 43: Float allocated_amount must be rejected immediately."""
        row = make_valid_postgrest_allocation_row()
        row["allocated_amount"] = 6.0
        with pytest.raises(FeeTaxAttributionTransportError, match="allocated_amount must be str or None"):
            canonicalize_fee_tax_attribution_postgrest_row(row)

    def test_integer_rejection_at_transport_boundary(self):
        """Item 44: Integer allocated_amount must be rejected immediately."""
        row = make_valid_postgrest_allocation_row()
        row["allocated_amount"] = 6
        with pytest.raises(FeeTaxAttributionTransportError, match="allocated_amount must be str or None"):
            canonicalize_fee_tax_attribution_postgrest_row(row)

    def test_decimal_instance_rejection_at_transport_boundary(self):
        """Decimal object must be rejected at transport boundary (PostgREST returns text)."""
        row = make_valid_postgrest_allocation_row()
        row["allocated_amount"] = Decimal("6.000")
        with pytest.raises(FeeTaxAttributionTransportError, match="allocated_amount must be str or None"):
            canonicalize_fee_tax_attribution_postgrest_row(row)

    def test_boolean_rejection_for_allocated_amount(self):
        """Boolean allocated_amount must be rejected."""
        row = make_valid_postgrest_allocation_row()
        row["allocated_amount"] = True
        with pytest.raises(FeeTaxAttributionTransportError, match="allocated_amount must be str or None"):
            canonicalize_fee_tax_attribution_postgrest_row(row)

    def test_utc_timestamp_preserved(self):
        """Item 45: UTC timestamp text canonical output identical."""
        row = make_valid_postgrest_allocation_row(recorded_at="2026-08-29T12:00:00+00:00")
        canonical = canonicalize_fee_tax_attribution_postgrest_row(row)
        assert canonical["recorded_at"] == "2026-08-29T12:00:00+00:00"

    def test_offset_timestamp_normalized_to_utc(self):
        """Item 46: Offset timestamp converted to canonical UTC representation."""
        row = make_valid_postgrest_allocation_row(recorded_at="2026-08-29T15:00:00+03:00")
        canonical = canonicalize_fee_tax_attribution_postgrest_row(row)
        assert canonical["recorded_at"] == "2026-08-29T12:00:00+00:00"

    def test_short_fraction_preservation(self):
        """Item 47: Short fractional seconds preserved with exact microsecond precision."""
        owner_id = UUID("d4444444-4444-4444-8444-444444444444")
        row = make_valid_postgrest_allocation_row(recorded_at="2026-08-29T12:00:00.123+00:00")
        canonical = canonicalize_fee_tax_attribution_postgrest_row(row)
        assert canonical["recorded_at"] == "2026-08-29T12:00:00.123000+00:00"

        event = hydrate_fee_tax_attribution_persistence_event(canonical, expected_owner_id=owner_id)
        assert event.recorded_at.microsecond == 123000

    def test_full_microsecond_preservation(self):
        """Item 48: Full 6-digit microsecond timestamp retained without rounding."""
        owner_id = UUID("d4444444-4444-4444-8444-444444444444")
        row = make_valid_postgrest_allocation_row(recorded_at="2026-08-29T12:00:00.123456+00:00")
        canonical = canonicalize_fee_tax_attribution_postgrest_row(row)
        assert canonical["recorded_at"] == "2026-08-29T12:00:00.123456+00:00"

        event = hydrate_fee_tax_attribution_persistence_event(canonical, expected_owner_id=owner_id)
        assert event.recorded_at.microsecond == 123456

    def test_z_timezone_transport_acceptance(self):
        """Item 49: PostgREST 'Z' suffix accepted and canonicalized to +00:00."""
        owner_id = UUID("d4444444-4444-4444-8444-444444444444")
        row = make_valid_postgrest_allocation_row(recorded_at="2026-08-29T12:00:00Z")
        canonical = canonicalize_fee_tax_attribution_postgrest_row(row)
        assert canonical["recorded_at"] == "2026-08-29T12:00:00+00:00"

        event = hydrate_fee_tax_attribution_persistence_event(canonical, expected_owner_id=owner_id)
        assert event.recorded_at.tzinfo == timezone.utc

    def test_naive_timestamp_rejection(self):
        """Item 50: Naive timestamp rejected."""
        row = make_valid_postgrest_allocation_row(recorded_at="2026-08-29T12:00:00")
        with pytest.raises(FeeTaxAttributionTransportError, match="must be timezone-aware"):
            canonicalize_fee_tax_attribution_postgrest_row(row)

    def test_datetime_object_in_transport_row_rejected(self):
        """Item 13: Datetime object directly in raw PostgREST row rejected."""
        row = make_valid_postgrest_allocation_row()
        row["recorded_at"] = datetime.now(timezone.utc)
        with pytest.raises(FeeTaxAttributionTransportError, match="recorded_at must be an ISO datetime string"):
            canonicalize_fee_tax_attribution_postgrest_row(row)

    def test_key_drift_rejection(self):
        """Item 51: Missing or extra keys rejected."""
        # Missing key
        row_missing = make_valid_postgrest_allocation_row()
        del row_missing["allocated_amount"]
        with pytest.raises(FeeTaxAttributionTransportError, match="missing required keys"):
            canonicalize_fee_tax_attribution_postgrest_row(row_missing)

        # Extra key
        row_extra = make_valid_postgrest_allocation_row()
        row_extra["currency"] = "USD"
        with pytest.raises(FeeTaxAttributionTransportError, match="unexpected extra keys"):
            canonicalize_fee_tax_attribution_postgrest_row(row_extra)

    def test_non_mapping_row_rejection(self):
        """Non-mapping inputs rejected."""
        for invalid in [None, "string", [1, 2, 3], (1, 2), 123, True]:
            with pytest.raises(FeeTaxAttributionTransportError, match="PostgREST row must be a Mapping"):
                canonicalize_fee_tax_attribution_postgrest_row(invalid)

    def test_red_team_end_to_end_transport_flow(self):
        """Item 71: End-to-end transport and hydration with large Decimal and offset datetime."""
        owner_id = UUID("d4444444-4444-4444-8444-444444444444")
        raw_row = make_valid_postgrest_allocation_row(
            allocated_amount="12345678901234567890.123400",
            recorded_at="2026-08-29T15:30:00.123+03:00",
        )
        canonical = canonicalize_fee_tax_attribution_postgrest_row(raw_row)
        assert canonical["allocated_amount"] == "12345678901234567890.123400"
        assert canonical["recorded_at"] == "2026-08-29T12:30:00.123000+00:00"

        event = hydrate_fee_tax_attribution_persistence_event(canonical, expected_owner_id=owner_id)
        assert event.allocated_amount == Decimal("12345678901234567890.123400")
        assert event.recorded_at.microsecond == 123000
        assert event.recorded_at.tzinfo == timezone.utc
        assert event.event_type == FeeTaxAttributionEventType.ALLOCATION
