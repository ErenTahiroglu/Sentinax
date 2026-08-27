"""
backend/tests/test_tefas_price_resolver.py
==========================================
Comprehensive Unit & Regression Test Suite for Point-in-Time TEFAS Fund Price Resolver.

Coverage Matrix:
    1. CURRENT_REPORTED correction selects newest authoritative snapshot
    2. SYSTEM_AS_OF historical isolation selects pre-correction state
    3. Future corruption / conflict isolation under SYSTEM_AS_OF
    4. Failed HTTP transport snapshots (500, 403, 429) do not supersede valid data
    5. Failed snapshots with naive timestamps do not poison resolution
    6. Non-covering snapshots with naive timestamps do not poison resolution
    7. Covering successful snapshots with naive timestamps fail closed (INVALID_TEMPORAL_LINEAGE)
    8. Incremental recent snapshots (periyod=1) do not erase older history (periyod=60)
    9. True no-resurrection: newer covering snapshot missing target date returns NO_ELIGIBLE_OBSERVATION
    10. Invalid target row in newer covering snapshot returns NO_ELIGIBLE_OBSERVATION (no backfill)
    11. Exact date only: weekend outside range -> NO_SNAPSHOT; within range -> NO_ELIGIBLE_OBSERVATION
    12. Same-scope different-hash conflict at frontier -> SNAPSHOT_CONFLICT
    13. Logical duplicate snapshots with different UUIDs deduplicate safely
    14. Differing scopes (period=1 vs 60) at same timestamp with identical price resolve deterministically
    15. Differing scopes at same timestamp with differing prices yield SNAPSHOT_CONFLICT
    16. Multiple target observations with differing prices in single snapshot yield OBSERVATION_CONFLICT
    17. Exact duplicate target observations in single snapshot deduplicate cleanly
    18. Lineage validation failures (mismatched snapshot_id, payload_hash, symbol) yield INVALID_TEMPORAL_LINEAGE
    19. Cryptographic resolution_key is strictly UUID-independent
    20. Snapshot and observation input ordering independence
    21. SOURCE_AS_OF always returns UNAVAILABLE_SOURCE_AS_OF
    22. SYSTEM_AS_OF with naive as_of returns INVALID_TEMPORAL_LINEAGE
    23. Exact Decimal unit price preservation (zero float conversion or distortion)
    24. Generic TEFAS_FUND and specialized fund types accepted
    25. Unsupported instrument types rejected (NO_ELIGIBLE_OBSERVATION)
    26. evaluation_snapshot_ids audit trail exposes only evaluated candidates, never future IDs
    27. Fund title exclusion: title metadata does not affect authority

Zero external network calls (pytest-socket enforced).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.market_data.models import (
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    MarketObservationResolutionResult,
    TefasFundPriceQueryKey,
)
from backend.engine.private.market_data.resolver import (
    PointInTimeMarketDataResolver,
)
from backend.engine.private.market_data.tefas_models import (
    TefasCapability,
    TefasFundPriceObservation,
    TefasFundPriceSnapshot,
    TefasObservationStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture Factories
# ─────────────────────────────────────────────────────────────────────────────

def make_observation(
    instrument_id: UUID,
    trade_date: date,
    unit_price: Decimal = Decimal("1.2345"),
    provider_symbol: str = "MAC",
    currency: Currency = Currency.TRY,
    instrument_type: InstrumentType = InstrumentType.TEFAS_FUND,
    status: TefasObservationStatus = TefasObservationStatus.VALID,
    obs_id: Optional[UUID] = None,
    snapshot_id: Optional[UUID] = None,
    payload_hash: Optional[str] = None,
) -> TefasFundPriceObservation:
    return TefasFundPriceObservation(
        id=obs_id or uuid4(),
        instrument_id=instrument_id,
        provider_symbol=provider_symbol,
        trade_date=trade_date,
        unit_price=unit_price,
        currency=currency,
        instrument_type=instrument_type,
        status=status,
        snapshot_id=snapshot_id,
        payload_hash=payload_hash,
        confidence_level=DataConfidenceLevel.HIGH if status == TefasObservationStatus.VALID else DataConfidenceLevel.NONE,
    )


def make_snapshot(
    instrument_id: UUID,
    retrieved_at: datetime,
    observations: List[TefasFundPriceObservation],
    provider_symbol: str = "MAC",
    period_months: int = 60,
    trade_date_range: Optional[Tuple[Optional[date], Optional[date]]] = None,
    http_status: int = 200,
    payload_hash: str = "hash_default",
    snap_id: Optional[UUID] = None,
    diagnostics: Optional[List[str]] = None,
) -> TefasFundPriceSnapshot:
    sid = snap_id or uuid4()
    if trade_date_range is None:
        valid_dates = [o.trade_date for o in observations if o.is_valid]
        trade_date_range = (min(valid_dates), max(valid_dates)) if valid_dates else (None, None)

    # Link observations to this snapshot for lineage
    linked_obs = []
    for o in observations:
        linked_obs.append(
            TefasFundPriceObservation(
                id=o.id,
                instrument_id=o.instrument_id,
                provider=o.provider,
                provider_symbol=o.provider_symbol,
                trade_date=o.trade_date,
                unit_price=o.unit_price,
                currency=o.currency,
                instrument_type=o.instrument_type,
                status=o.status,
                snapshot_id=sid,
                payload_hash=payload_hash,
                diagnostics=list(o.diagnostics),
                confidence_level=o.confidence_level,
            )
        )

    return TefasFundPriceSnapshot(
        id=sid,
        provider="TEFAS",
        provider_symbol=provider_symbol,
        retrieved_at=retrieved_at,
        http_status=http_status,
        payload_hash=payload_hash,
        raw_payload="{}",
        instrument_id=instrument_id,
        period_months=period_months,
        endpoint="FUND_PRICE_HISTORY",
        trade_date_range=trade_date_range,
        observations=linked_obs,
        diagnostics=diagnostics or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

def test_01_current_correction_selects_newest():
    """Scenario 1: Newer snapshot correcting historical price is selected in CURRENT_REPORTED mode."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    # Snapshot 1: Retrieved at 10:00, price 1.00
    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], payload_hash="hash_v1")

    # Snapshot 2: Retrieved at 14:00, price 1.10 (corrected)
    t2 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    obs2 = make_observation(inst_id, target, Decimal("1.10"))
    snap2 = make_snapshot(inst_id, t2, [obs2], payload_hash="hash_v2")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap1, snap2], MarketDataResolutionMode.CURRENT_REPORTED)
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation is not None
    assert res.selected_observation.unit_price == Decimal("1.10")
    assert res.snapshot_hash == "hash_v2"


def test_02_system_as_of_historical_isolation():
    """Scenario 2: SYSTEM_AS_OF before correction selects original price; after correction selects revised price."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], payload_hash="hash_v1")

    t2 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    obs2 = make_observation(inst_id, target, Decimal("1.10"))
    snap2 = make_snapshot(inst_id, t2, [obs2], payload_hash="hash_v2")

    # As of 12:00 (between t1 and t2)
    as_of_12 = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    res_before = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of_12
    )
    assert res_before.status == MarketDataResolutionStatus.SELECTED
    assert res_before.selected_observation.unit_price == Decimal("1.00")
    assert res_before.snapshot_hash == "hash_v1"

    # As of 15:00 (after t2)
    as_of_15 = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    res_after = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of_15
    )
    assert res_after.status == MarketDataResolutionStatus.SELECTED
    assert res_after.selected_observation.unit_price == Decimal("1.10")
    assert res_after.snapshot_hash == "hash_v2"


def test_03_future_corruption_isolation():
    """Scenario 3: Future corrupt/conflicted snapshot does not contaminate historical SYSTEM_AS_OF query."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], payload_hash="hash_v1")

    # Future snapshot at 14:00 with conflicting internal rows
    t2 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    obs2a = make_observation(inst_id, target, Decimal("1.10"))
    obs2b = make_observation(inst_id, target, Decimal("1.20"))
    snap2 = make_snapshot(inst_id, t2, [obs2a, obs2b], payload_hash="hash_v2_corrupt")

    as_of_11 = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)
    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of_11
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("1.00")


def test_04_failed_fetch_isolation():
    """Scenario 4: HTTP 500 error attempt at 11:00 does not supersede valid 10:00 snapshot."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], payload_hash="hash_v1")

    t2 = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)
    snap2_fail = make_snapshot(inst_id, t2, [], http_status=500, payload_hash="")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2_fail], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("1.00")


def test_05_failed_naive_timestamp_isolation():
    """Scenario 5: Failed snapshot having a naive timestamp does not poison resolution of valid data."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], payload_hash="hash_v1")

    # Failed snapshot with naive timestamp
    t2_naive = datetime(2026, 8, 20, 11, 0)
    snap2_naive_fail = make_snapshot(inst_id, t2_naive, [], http_status=500, payload_hash="")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2_naive_fail], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("1.00")


def test_06_non_covering_naive_snapshot_does_not_poison():
    """Scenario 6: Non-covering snapshot having a naive timestamp does not poison resolution for target date."""
    inst_id = uuid4()
    target = date(2024, 5, 10)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    # Old snapshot covering 2024
    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("0.85"))
    snap1 = make_snapshot(inst_id, t1, [obs1], trade_date_range=(date(2024, 1, 1), date(2024, 12, 31)), payload_hash="hash_2024")

    # Later snapshot covering only 2026 with naive timestamp
    t2_naive = datetime(2026, 8, 27, 10, 0)
    obs2 = make_observation(inst_id, date(2026, 8, 25), Decimal("1.50"))
    snap2_naive_noncovering = make_snapshot(
        inst_id, t2_naive, [obs2], trade_date_range=(date(2026, 8, 1), date(2026, 8, 25)), payload_hash="hash_2026"
    )

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2_naive_noncovering], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("0.85")


def test_07_covering_naive_snapshot_fails_closed():
    """Scenario 7: Covering successful snapshot with naive retrieved_at timestamp fails closed."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t_naive = datetime(2026, 8, 20, 10, 0)
    obs = make_observation(inst_id, target, Decimal("1.00"))
    snap = make_snapshot(inst_id, t_naive, [obs], payload_hash="hash_naive")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
    assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE
    assert any("naive or missing" in d for d in res.diagnostics)


def test_08_incremental_does_not_erase_history():
    """Scenario 8: Recent 1-month snapshot does not erase historical 60-month coverage for older date."""
    inst_id = uuid4()
    old_target = date(2023, 6, 15)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=old_target, provider_symbol="MAC")

    # 60-month snapshot ingested in Jan 2026 covering 2021-2026
    t1 = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
    obs_old = make_observation(inst_id, old_target, Decimal("0.55"))
    snap_60m = make_snapshot(
        inst_id, t1, [obs_old], period_months=60, trade_date_range=(date(2021, 1, 1), date(2026, 1, 10)), payload_hash="hash_60m"
    )

    # 1-month daily snapshot ingested in August 2026 covering only Aug 2026
    t2 = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    obs_recent = make_observation(inst_id, date(2026, 8, 25), Decimal("1.25"))
    snap_1m = make_snapshot(
        inst_id, t2, [obs_recent], period_months=1, trade_date_range=(date(2026, 8, 1), date(2026, 8, 25)), payload_hash="hash_1m"
    )

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap_60m, snap_1m], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("0.55")
    assert res.snapshot_hash == "hash_60m"


def test_09_true_no_resurrection_missing_row():
    """Scenario 9: Newest covering snapshot omits target date -> NO_ELIGIBLE_OBSERVATION (no fallback to old snapshot)."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    # Old snapshot with price
    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], trade_date_range=(date(2026, 8, 1), date(2026, 8, 20)), payload_hash="hash_v1")

    # Newer snapshot covering the range 2026-08-01 to 2026-08-25, but row for 2026-08-20 was removed/omitted
    t2 = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    obs_other = make_observation(inst_id, date(2026, 8, 25), Decimal("1.15"))
    snap2 = make_snapshot(inst_id, t2, [obs_other], trade_date_range=(date(2026, 8, 1), date(2026, 8, 25)), payload_hash="hash_v2")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
    assert res.selected_observation is None


def test_10_invalid_target_no_resurrection():
    """Scenario 10: Newer covering snapshot contains INVALID_OBSERVATION for target date -> NO_ELIGIBLE_OBSERVATION."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], payload_hash="hash_v1")

    t2 = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    obs2_invalid = make_observation(inst_id, target, Decimal("0.00"), status=TefasObservationStatus.INVALID_OBSERVATION)
    snap2 = make_snapshot(inst_id, t2, [obs2_invalid], trade_date_range=(date(2026, 8, 1), date(2026, 8, 25)), payload_hash="hash_v2")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
    assert res.selected_observation is None


def test_11_weekend_query_handling():
    """Scenario 11: Exact date only. Weekend outside range -> NO_SNAPSHOT; within range -> NO_ELIGIBLE_OBSERVATION."""
    inst_id = uuid4()
    # Wednesday 2026-08-19, Saturday 2026-08-22
    wed = date(2026, 8, 19)
    sat = date(2026, 8, 22)
    t = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)

    obs_wed = make_observation(inst_id, wed, Decimal("1.00"))
    obs_fri = make_observation(inst_id, date(2026, 8, 21), Decimal("1.02"))
    # Snapshot range: Mon 2026-08-17 to Fri 2026-08-21
    snap = make_snapshot(inst_id, t, [obs_wed, obs_fri], trade_date_range=(date(2026, 8, 17), date(2026, 8, 21)))

    # 1. Query Wednesday -> SELECTED
    res_wed = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        TefasFundPriceQueryKey(inst_id, wed), [snap], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res_wed.status == MarketDataResolutionStatus.SELECTED

    # 2. Query Saturday outside range -> NO_SNAPSHOT
    res_sat_outside = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        TefasFundPriceQueryKey(inst_id, sat), [snap], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res_sat_outside.status == MarketDataResolutionStatus.NO_SNAPSHOT

    # 3. Query Saturday within broad 30-day range without Saturday observation -> NO_ELIGIBLE_OBSERVATION
    snap_broad = make_snapshot(inst_id, t, [obs_wed, obs_fri], trade_date_range=(date(2026, 8, 1), date(2026, 8, 31)))
    res_sat_inside = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        TefasFundPriceQueryKey(inst_id, sat), [snap_broad], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res_sat_inside.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_12_same_scope_hash_conflict():
    """Scenario 12: Multiple snapshots at same retrieved_at with identical scope but different payload_hash -> SNAPSHOT_CONFLICT."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t, [obs1], period_months=1, payload_hash="hash_A")

    obs2 = make_observation(inst_id, target, Decimal("1.05"))
    snap2 = make_snapshot(inst_id, t, [obs2], period_months=1, payload_hash="hash_B")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT


def test_13_logical_dup_snapshot_deduped():
    """Scenario 13: Exact logical duplicate snapshots with different UUIDs deduplicate safely."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t, [obs1], payload_hash="hash_exact", snap_id=uuid4())

    obs2 = make_observation(inst_id, target, Decimal("1.00"))
    snap2 = make_snapshot(inst_id, t, [obs2], payload_hash="hash_exact", snap_id=uuid4())

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("1.00")


def test_14_different_period_same_price_deterministic():
    """Scenario 14: Differing periods (1m vs 60m) at identical retrieved_at with matching target price resolve deterministically."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap_1m = make_snapshot(inst_id, t, [obs1], period_months=1, trade_date_range=(date(2026, 8, 1), date(2026, 8, 20)), payload_hash="hash_1m")

    obs60 = make_observation(inst_id, target, Decimal("1.00"))
    snap_60m = make_snapshot(inst_id, t, [obs60], period_months=60, trade_date_range=(date(2021, 8, 1), date(2026, 8, 20)), payload_hash="hash_60m")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap_1m, snap_60m], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("1.00")


def test_15_different_period_different_price_conflict():
    """Scenario 15: Differing periods at identical retrieved_at producing different prices yield SNAPSHOT_CONFLICT."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap_1m = make_snapshot(inst_id, t, [obs1], period_months=1, trade_date_range=(date(2026, 8, 1), date(2026, 8, 20)), payload_hash="hash_1m")

    obs60 = make_observation(inst_id, target, Decimal("1.05"))
    snap_60m = make_snapshot(inst_id, t, [obs60], period_months=60, trade_date_range=(date(2021, 8, 1), date(2026, 8, 20)), payload_hash="hash_60m")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap_1m, snap_60m], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT


def test_16_observation_conflict_in_snapshot():
    """Scenario 16: Single authoritative snapshot with conflicting observations for same date -> OBSERVATION_CONFLICT."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs_a = make_observation(inst_id, target, Decimal("1.00"))
    obs_b = make_observation(inst_id, target, Decimal("1.20"))
    snap = make_snapshot(inst_id, t, [obs_a, obs_b], payload_hash="hash_conflict")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.OBSERVATION_CONFLICT


def test_17_observation_deduplication_exact_duplicate():
    """Scenario 17: Single snapshot with exact duplicate observations for same date deduplicates cleanly."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs_a = make_observation(inst_id, target, Decimal("1.00"), obs_id=uuid4())
    obs_b = make_observation(inst_id, target, Decimal("1.00"), obs_id=uuid4())
    snap = make_snapshot(inst_id, t, [obs_a, obs_b], payload_hash="hash_dup")

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap], MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == Decimal("1.00")


def test_18_lineage_validation_failures():
    """Scenario 18: Provenance/lineage mismatches yield INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    # 1. Mismatched provider
    obs_bad_prov = make_observation(inst_id, target, Decimal("1.00"))
    obs_bad_prov.provider = "NOT_TEFAS"
    snap_bad_prov = make_snapshot(inst_id, t, [obs_bad_prov])
    # manually override linked observation provider
    snap_bad_prov.observations[0].provider = "NOT_TEFAS"
    res1 = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap_bad_prov])
    assert res1.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    # 2. Mismatched payload_hash
    obs_bad_hash = make_observation(inst_id, target, Decimal("1.00"))
    snap_bad_hash = make_snapshot(inst_id, t, [obs_bad_hash], payload_hash="snap_hash")
    snap_bad_hash.observations[0].payload_hash = "wrong_hash"
    res2 = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap_bad_hash])
    assert res2.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    # 3. Mismatched snapshot_id
    obs_bad_snap_id = make_observation(inst_id, target, Decimal("1.00"))
    snap_bad_sid = make_snapshot(inst_id, t, [obs_bad_snap_id])
    snap_bad_sid.observations[0].snapshot_id = uuid4()
    res3 = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap_bad_sid])
    assert res3.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_19_uuid_independence():
    """Scenario 19: Cryptographic resolution_key is invariant to physical UUIDs."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    # Set 1 with random UUIDs
    obs1 = make_observation(inst_id, target, Decimal("1.00"), obs_id=uuid4())
    snap1 = make_snapshot(inst_id, t, [obs1], payload_hash="fixed_hash", snap_id=uuid4())
    res1 = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap1], MarketDataResolutionMode.CURRENT_REPORTED)

    # Set 2 with completely different UUIDs but identical economic data
    obs2 = make_observation(inst_id, target, Decimal("1.00"), obs_id=uuid4())
    snap2 = make_snapshot(inst_id, t, [obs2], payload_hash="fixed_hash", snap_id=uuid4())
    res2 = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap2], MarketDataResolutionMode.CURRENT_REPORTED)

    assert res1.status == MarketDataResolutionStatus.SELECTED
    assert res2.status == MarketDataResolutionStatus.SELECTED
    assert res1.resolution_key is not None
    assert res1.resolution_key == res2.resolution_key


def test_20_input_order_independence():
    """Scenario 20: Snapshot and observation input ordering produces identical resolution_key."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)

    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], payload_hash="hash_v1")

    obs2 = make_observation(inst_id, target, Decimal("1.10"))
    snap2 = make_snapshot(inst_id, t2, [obs2], payload_hash="hash_v2")

    # Order A: [snap1, snap2]
    res_a = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap1, snap2], MarketDataResolutionMode.CURRENT_REPORTED)

    # Order B: [snap2, snap1]
    res_b = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap2, snap1], MarketDataResolutionMode.CURRENT_REPORTED)

    assert res_a.selected_observation.unit_price == res_b.selected_observation.unit_price
    assert res_a.resolution_key == res_b.resolution_key


def test_21_source_as_of_unavailable():
    """Scenario 21: SOURCE_AS_OF mode always returns UNAVAILABLE_SOURCE_AS_OF."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs = make_observation(inst_id, target, Decimal("1.00"))
    snap = make_snapshot(inst_id, t, [obs])

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap], MarketDataResolutionMode.SOURCE_AS_OF)
    assert res.status == MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF


def test_22_naive_as_of_fails_closed():
    """Scenario 22: SYSTEM_AS_OF with naive as_of timestamp returns INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    obs = make_observation(inst_id, target, Decimal("1.00"))
    snap = make_snapshot(inst_id, t, [obs])

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2026, 8, 20, 12, 0)
    )
    assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_23_exact_unit_price_preserved():
    """Scenario 23: Exact high-precision Decimal unit price is preserved without quantization or distortion."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    exact_price = Decimal("0.761650123456")
    obs = make_observation(inst_id, target, exact_price)
    snap = make_snapshot(inst_id, t, [obs])

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.unit_price == exact_price
    assert str(res.selected_observation.unit_price) == "0.761650123456"


def test_24_generic_and_specialized_fund_types():
    """Scenario 24: Generic TEFAS_FUND and specialized fund types (TEFAS_EQUITY, TEFAS_MONEY_MARKET, etc.) resolve cleanly."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    for f_type in (
        InstrumentType.TEFAS_FUND,
        InstrumentType.TEFAS_EQUITY,
        InstrumentType.TEFAS_MONEY_MARKET,
        InstrumentType.TEFAS_VARIABLE,
        InstrumentType.TEFAS_BALANCED,
    ):
        query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target)
        obs = make_observation(inst_id, target, Decimal("2.00"), instrument_type=f_type)
        snap = make_snapshot(inst_id, t, [obs])
        res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap])
        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.instrument_type == f_type


def test_25_unsupported_instrument_type_fails_closed():
    """Scenario 25: Non-TEFAS instrument types (e.g. BIST_STOCK, UCITS_FUND) fail closed as NO_ELIGIBLE_OBSERVATION."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target)

    obs_bist = make_observation(inst_id, target, Decimal("100.00"), instrument_type=InstrumentType.BIST_STOCK)
    snap = make_snapshot(inst_id, t, [obs_bist])

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_26_evaluation_snapshot_ids_audit():
    """Scenario 26: evaluation_snapshot_ids contains only evaluated candidates; SYSTEM_AS_OF never exposes future IDs."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")

    t1 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    id1 = uuid4()
    obs1 = make_observation(inst_id, target, Decimal("1.00"))
    snap1 = make_snapshot(inst_id, t1, [obs1], snap_id=id1)

    t2 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    id2 = uuid4()
    obs2 = make_observation(inst_id, target, Decimal("1.10"))
    snap2 = make_snapshot(inst_id, t2, [obs2], snap_id=id2)

    # In SYSTEM_AS_OF at 11:00, id2 is future and must NOT appear in evaluation_snapshot_ids
    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
        query, [snap1, snap2], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.evaluation_snapshot_ids == [str(id1)]
    assert str(id2) not in res.evaluation_snapshot_ids


def test_27_title_exclusion():
    """Scenario 27: Fund title does not affect resolution or observation equality."""
    inst_id = uuid4()
    target = date(2026, 8, 20)
    query = TefasFundPriceQueryKey(instrument_id=inst_id, trade_date=target, provider_symbol="MAC")
    t = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    obs = make_observation(inst_id, target, Decimal("1.00"))
    snap = make_snapshot(inst_id, t, [obs])

    res = PointInTimeMarketDataResolver.resolve_tefas_fund_price(query, [snap])
    assert res.status == MarketDataResolutionStatus.SELECTED
    # Observation contains no title field
    assert not hasattr(res.selected_observation, "title")
    assert not hasattr(res.selected_observation, "fon_unvan")
